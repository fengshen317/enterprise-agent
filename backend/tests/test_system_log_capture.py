"""用真实 PostgreSQL 验证诊断采集、隐私边界和写入降级。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.agents.provider import DeepSeekProvider
from app.models.connector import Connector
from app.models.identity import Workspace
from app.services.system_logs import SystemLogRecorder
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration
_LOGS = "/api/admin/system-logs"
_PRIVATE = "private-log-fixture-never-store"
_IMA_TOOL = "ima__search_knowledge_base"


async def test_ima_failure_correlates_request_run_and_tool_without_payloads(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实走问答、工具审计、ima 解析和后台查询，外部响应使用测试传输。

    Args:
        chat: 独立真实 PostgreSQL 与已登录应用。
        monkeypatch: 限定模型、上游传输及测试凭证。
    """
    app, admin, _ = chat
    assert logging.getLogger("uvicorn.access").disabled
    runtime = app.state.chat_runtime
    monkeypatch.setattr(runtime.settings, "connector_credential_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(runtime.settings, "deepseek_api_key", SecretStr(_PRIVATE))
    runtime.provider = DeepSeekProvider(runtime.settings)
    tool_result: dict[str, Any] = {}
    original_post = httpx.AsyncClient.post

    async def transport(client: httpx.AsyncClient, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """仅替换 ima 官方 endpoint 的测试响应，应用 HTTP 调用保持原流程。"""
        if str(url) == "https://ima.qq.com/openapi/wiki/v1/search_knowledge_base":
            return httpx.Response(
                200, json={"code": 0, "data": {"info_list": [{"new_field": _PRIVATE}]}, "msg": _PRIVATE}
            )
        return await original_post(client, url, **kwargs)

    async def stream(
        provider: DeepSeekProvider,
        history: list[tuple[str, str]],
        *,
        images: tuple[tuple[str, bytes], ...] = (),
        on_preview: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        """由测试模型发起一次真实工具执行，工具失败后继续回答。"""
        assert history[-1][1] == _PRIVATE
        assert not images and on_preview is not None
        assert provider.tool_executor is not None
        tool_result.update(json.loads(await provider.tool_executor.invoke(_IMA_TOOL, {})))
        yield "工具返回格式错误，已结束排查。"

    monkeypatch.setattr(httpx.AsyncClient, "post", transport)
    monkeypatch.setattr(DeepSeekProvider, "stream", stream)
    async with app.state.database.session_factory() as session:
        assert await session.scalar(select(Connector.id).where(Connector.key == "ima")) is None
    try:
        assert (await admin.post("/api/admin/connectors/ima/install")).is_success
        assert (await admin.post("/api/admin/connectors/ima/enabled", json={"enabled": True})).is_success
        assert (
            await admin.put(
                "/api/connectors/ima/my-account",
                json={"credentials": {"client_id": "test-client", "api_key": _PRIVATE}},
            )
        ).is_success
        conversation = await new_conversation(admin)
        accepted = await admin.post(
            f"/api/conversations/{conversation}/runs", json={"content": _PRIVATE, "idempotency_key": "diagnostic"}
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run"]["id"]
        request_id = accepted.headers["X-Request-ID"]
        assert (await settled(admin, run_id))["run"]["status"] == "completed"
        assert tool_result["error_code"] == "CONNECTOR_INVALID_OUTPUT"
        tool_call_id = tool_result["audit_id"]
        async with asyncio.timeout(5):
            while True:
                listing = await admin.get(_LOGS, params={"q": run_id})
                assert listing.status_code == 200
                items = listing.json()["items"]
                if {"http_request_accepted", "run_started", "tool_finished", "run_finished"} <= {
                    row["event"] for row in items
                }:
                    break
                await asyncio.sleep(0.02)
        response = await admin.get(_LOGS, params={"q": tool_call_id, "component": "connector", "outcome": "error"})
        assert response.status_code == 200
        assert _PRIVATE not in response.text
        (event,) = response.json()["items"]
        assert event["request_id"] == request_id and event["run_id"] == run_id
        assert event["connector_key"] == "ima" and event["tool_name"] == _IMA_TOOL
        assert event["details"] == {
            "phase": "response_validation",
            "rule": "knowledge_base_id",
            "upstream_status": 200,
            "upstream_code": 0,
        }
        path = f"{_LOGS}/{event['id']}"
        detail = await admin.get(path)
        assert detail.status_code == 200 and _PRIVATE not in detail.text
        assert {"http_request_accepted", "run_started", "run_finished"} <= {
            row["event"] for row in detail.json()["related"]
        }
        async with app.state.database.session_factory() as session:
            slug = await session.scalar(select(Workspace.slug).where(Workspace.id == app.state.test_scope.workspace_id))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://member") as member:
            await add_identity(app, member, workspace=slug, role="member")
            assert (await member.get(path)).status_code == 403
            assert (await member.get(_LOGS)).status_code == 403
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other") as foreign:
            await add_identity(app, foreign)
            assert (await foreign.get(path)).status_code == 404
            assert (await foreign.get(_LOGS, params={"q": tool_call_id})).json()["items"] == []
    finally:
        async with app.state.database.session_factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "ima"))


async def test_diagnostic_writer_failure_leaves_chat_operational(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台日志无法写入时，真实库中的问答仍可完成。

    Args:
        chat: 独立真实 PostgreSQL 和应用。
        monkeypatch: 仅断开诊断写入的会话工厂。
    """
    app, client, _ = chat
    recorder: SystemLogRecorder = app.state.system_log_recorder
    failed = asyncio.Event()
    discard = recorder._discard

    def signal_failure(count: int, reason: str) -> None:
        """等待真实写入器进入其降级分支。"""
        discard(count, reason)
        if reason == "write_failed":
            failed.set()

    monkeypatch.setattr(recorder, "_discard", signal_failure)
    monkeypatch.setattr(recorder, "factory", object())
    started = await submit(client, await new_conversation(client))
    assert (await settled(client, started["run"]["id"]))["run"]["status"] == "completed"
    await asyncio.wait_for(failed.wait(), timeout=5)
    assert recorder.write_failure_count >= 1
    assert recorder.dropped_count >= 1
