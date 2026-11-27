"""连接器监控的 HTTP 契约与授权权限回归。"""

from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.connectors.registry import ConnectorDescriptor
from app.models.config import IntegrationConfig
from app.models.connector import Connector
from app.models.identity import Workspace
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration


async def test_health_contract_and_personal_authorization(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表公开脱敏状态，授权入口使用当前成员身份，安装和健康检查仍归管理员。

    Args:
        chat: 带真实登录身份的独立数据库应用。
        monkeypatch: 替换授权进程边界，防止测试触发真实授权。
    """
    app, client, _ = chat
    registry = app.state.connector_registry
    registry._descriptors["tmeet"] = ConnectorDescriptor(
        key="tmeet",
        kind="cli",
        name="腾讯会议",
        description="",
        icon="",
        enabled=True,
        status="error",
        tool_count=2,
        auth_status="required",
        last_error_code="not_logged_in",
        last_error="需要完成账号授权",
    )
    result = {
        "auth_url": "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=test-only",
        "output": "请完成授权",
        "already_logged_in": False,
        "state": "awaiting_user",
        "expires_at": None,
    }
    authorize = AsyncMock(return_value=result)
    monkeypatch.setattr(registry.personal_auth, "authorize", authorize)
    monkeypatch.setattr(registry.personal_auth, "status", lambda key, scope: result)
    listed = await client.get("/api/connectors")
    assert listed.status_code == 200
    item = next(row for row in listed.json() if row["key"] == "tmeet")
    assert item["auth_status"] == "required" and item["auth_action"] == "authorize"
    assert item["last_error_code"] == "not_logged_in"
    assert "auth_url" not in item
    assert "last_checked_at" in item and "last_success_at" in item
    authorized = await client.post("/api/admin/connectors/tmeet/auth")
    assert authorized.status_code == 200 and authorized.json() == result
    polling = await client.get("/api/admin/connectors/tmeet/auth/status")
    assert polling.status_code == 200 and polling.json()["state"] == "awaiting_user"
    assert polling.headers["Cache-Control"] == "no-store"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://member") as member:
        async with app.state.database.session_factory() as session:
            workspace = await session.scalar(
                select(Workspace.slug).where(Workspace.id == app.state.test_scope.workspace_id)
            )
        scope = await add_identity(app, member, workspace=workspace, role="member")
        assert (await member.get("/api/connectors")).status_code == 200
        assert (await member.post("/api/connectors/tmeet/my-account/auth")).status_code == 200
        assert (await member.get("/api/connectors/tmeet/my-account/auth/status")).status_code == 200
    assert authorize.await_count == 2
    authorize.assert_awaited_with("tmeet", scope, poll=False)


async def test_manual_health_check_keeps_existing_instance(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动检查返回健康字段，并通过现有实例探测入口完成。

    Args:
        chat: 独立数据库应用。
        monkeypatch: 隔离外部网络检查。
    """
    app, client, _ = chat
    registry = app.state.connector_registry
    registry._descriptors["tmeet"] = ConnectorDescriptor(
        key="tmeet",
        kind="cli",
        name="腾讯会议",
        description="",
        icon="",
        enabled=True,
        status="degraded",
        auth_status="unknown",
        tool_count=2,
        last_error_code="network_error",
    )
    check = AsyncMock()
    refresh = AsyncMock()
    monkeypatch.setattr(registry, "check_health", check)
    monkeypatch.setattr(registry, "refresh", refresh)
    response = await client.post("/api/admin/connectors/tmeet/test")
    assert response.status_code == 200
    assert response.json()["auth_action"] == "retry"
    assert response.json()["auth_status"] == "unknown"
    check.assert_awaited_once_with("tmeet")
    refresh.assert_not_awaited()


async def test_configured_api_key_is_used_and_hidden_through_normal_api(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理员配置密钥后，真实注册表使用凭证检查健康，停用清除提醒。

    Args:
        chat: 真实测试数据库与管理员客户端。
        monkeypatch: 隔离外部健康接口。
    """
    app, client, _ = chat
    key = "monitor_api_" + uuid4().hex
    secret_name = f"connector.{key}.api_key"
    registry = app.state.connector_registry
    factory = app.state.database.session_factory

    def transport(request: httpx.Request) -> httpx.Response:
        """验证健康检查实际使用新保存的后台密钥。

        Args:
            request: 只读请求。

        Returns:
            健康接口结果。
        """
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer qa-private-api-key"
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(
        "app.connectors.api.httpx.AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(transport)),
    )
    try:
        async with factory.begin() as session:
            session.add(
                Connector(
                    key=key,
                    kind="api",
                    name="健康配置验证",
                    enabled=True,
                    config={"base_url": "https://example.invalid", "requires_api_key": True},
                )
            )
        await registry.refresh(key)
        before = (await client.get(f"/api/connectors/{key}")).json()
        assert before["auth_status"] == "required" and before["auth_action"] == "configure"
        saved = await client.put(
            f"/api/admin/connectors/{key}/config",
            json={
                "config": {"health_path": "/health"},
                "secrets": {"api_key": "qa-private-api-key"},
            },
        )
        assert saved.status_code == 200
        assert saved.json()["auth_status"] == "authorized"
        assert saved.json()["auth_action"] is None and saved.json()["last_success_at"] is not None
        assert "qa-private-api-key" not in saved.text
        disabled = await client.post(f"/api/admin/connectors/{key}/enabled", json={"enabled": False})
        assert disabled.status_code == 200
        assert not disabled.json()["enabled"] and disabled.json()["auth_action"] is None
        assert registry.resolve(key) is None
        enabled = await client.post(f"/api/admin/connectors/{key}/enabled", json={"enabled": True})
        assert enabled.status_code == 200 and enabled.json()["auth_status"] == "authorized"
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == key))
            await session.execute(delete(IntegrationConfig).where(IntegrationConfig.key == secret_name))
        await registry.refresh(key)
