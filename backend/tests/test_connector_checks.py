"""验证主动检查的授权、独立探针与安全诊断。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.api import connectors
from app.connectors.base import ConnectorError
from app.core.errors import ApplicationError
from app.models.connector import Connector
from app.models.identity import Workspace
from app.schemas.auth import ResourceScope
from app.schemas.connectors import ConnectorTestResult
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider
from tests.test_chat_integration import chat as chat


@pytest.mark.integration
async def test_ima_check_uses_only_current_members_binding(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实鉴权和密文查询仅允许当前成员验证已启用连接器。"""
    app, admin, _ = chat
    factory = app.state.database.session_factory
    owner = app.state.test_scope
    monkeypatch.setattr(app.state.settings, "connector_credential_key", SecretStr(Fernet.generate_key().decode()))
    diagnostics = {"phase": "auth", "upstream_status": 403}
    diagnostic_id = uuid4()
    invoke = AsyncMock(
        side_effect=[
            {"items": []},
            ConnectorError("permission_denied", "private-response", diagnostics=diagnostics),
        ]
    )
    emit = MagicMock(side_effect=[None, diagnostic_id])
    monkeypatch.setattr(connectors.ima, "invoke", invoke)
    monkeypatch.setattr(connectors, "emit_system_log", emit)
    async with factory() as session:
        assert not await session.scalar(select(Connector.id).where(Connector.key == "ima"))
        slug = await session.scalar(select(Workspace.slug).where(Workspace.id == owner.workspace_id))

    manage = "/api/admin/connectors/ima"
    account = "/api/connectors/ima/my-account"
    values = {"client_id": "test-client", "api_key": "private-credential"}
    try:
        assert (await admin.post(manage + "/install")).is_success
        assert (await admin.post(manage + "/enabled", json={"enabled": True})).is_success
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as member:
            scope = await add_identity(app, member, workspace=slug)
            assert (await member.put(account, json={"credentials": values})).is_success
            assert (await admin.post(account + "/check")).status_code == 409
            invoke.assert_not_awaited()

            result = await member.post(account + "/check")
            assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
            assert result.json() == {
                "knowledge_base": {"status": "success", "error_code": None, "diagnostic_id": None},
                "notes": {"status": "error", "error_code": "permission_denied", "diagnostic_id": str(diagnostic_id)},
            }
            calls = invoke.await_args_list
            assert [call.args[0] for call in calls] == ["search_knowledge_base", "search_note"]
            assert calls[0].args[1] == {"limit": 1}
            assert calls[1].args[1] == {"query": connectors._IMA_NOTE_PROBE_QUERY, "limit": 1}
            assert all(call.args[2] == values for call in calls)
            events = [call.kwargs for call in emit.call_args_list]
            common = {
                "component": "connector",
                "event": "connector_check",
                "connector_key": "ima",
                "workspace_id": scope.workspace_id,
            }
            for event in events:
                assert isinstance(event.pop("duration_ms"), int)
            assert events == [
                {
                    **common,
                    "tool_name": "search_knowledge_base",
                    "outcome": "success",
                    "error_code": None,
                    "details": {},
                },
                {
                    **common,
                    "tool_name": "search_note",
                    "outcome": "error",
                    "error_code": "PERMISSION_DENIED",
                    "details": diagnostics,
                },
            ]

            assert (await admin.post(manage + "/enabled", json={"enabled": False})).is_success
            assert (await member.post(account + "/check")).status_code == 409
            assert invoke.await_count == 2
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "ima"))
        await app.state.connector_registry.refresh("ima")


async def test_ima_probe_deadline_records_transport_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针调用挂起时返回安全的超时状态和诊断编号。"""
    monkeypatch.setattr(connectors, "_IMA_PROBE_TIMEOUT_SECONDS", 0)

    async def wait_for_timeout(*_args: object) -> None:
        """保持上游挂起，直到整体超时取消该探针。"""
        await asyncio.Event().wait()

    monkeypatch.setattr(connectors.ima, "invoke", wait_for_timeout)
    diagnostic_id = uuid4()
    emit = MagicMock(return_value=diagnostic_id)
    monkeypatch.setattr(connectors, "emit_system_log", emit)
    result = await connectors._probe_ima("search_note", {}, {}, uuid4())
    assert result.status == "error" and result.error_code == "timeout" and result.diagnostic_id == diagnostic_id
    assert emit.call_args.kwargs["details"] == {"phase": "transport"}


@pytest.mark.parametrize(
    ("key", "status", "last_error_code", "outcome", "error_code"),
    [
        ("tmeet", "connected", None, "success", None),
        ("tmeet", "degraded", "network_error", "error", "NETWORK_ERROR"),
        ("tmeet", "disabled", None, "warning", "CONNECTOR_DISABLED"),
        ("ima", "connected", None, "warning", "PERSONAL_CHECK_REQUIRED"),
    ],
)
async def test_admin_connector_check_preserves_response_and_reports_outcome(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    status: str,
    last_error_code: str | None,
    outcome: str,
    error_code: str | None,
) -> None:
    """仅已授权的手动检查投递健康状态，原有返回值保持兼容。"""
    request = Request({"type": "http"})
    scope = ResourceScope(uuid4(), uuid4())
    result = ConnectorTestResult(status=status, message="test", last_error_code=last_error_code)
    check = AsyncMock(return_value=result)
    require_admin = AsyncMock()
    emit = MagicMock()
    monkeypatch.setattr(connectors, "service", lambda _: SimpleNamespace(test_connection=check))
    monkeypatch.setattr(connectors, "_require_admin", require_admin)
    monkeypatch.setattr(connectors, "emit_system_log", emit)
    assert await connectors.test_connection(key, request, scope) is result
    require_admin.assert_awaited_once_with(request, scope)
    check.assert_awaited_once_with(key)
    event = emit.call_args.kwargs
    assert isinstance(event.pop("duration_ms"), int)
    assert event == {
        "component": "connector",
        "event": "connector_check",
        "workspace_id": scope.workspace_id,
        "connector_key": key,
        "outcome": outcome,
        "error_code": error_code,
    }

    require_admin.side_effect = ApplicationError("FORBIDDEN", "无权检查", 403)
    with pytest.raises(ApplicationError, match="无权检查"):
        await connectors.test_connection(key, request, scope)
    assert check.await_count == emit.call_count == 1
