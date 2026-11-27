"""通过真实登录和数据库验证普通成员的独立扫码授权。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.connectors import personal_authorization, yangjibao_authorization
from app.connectors.cli import CliConnector
from app.connectors.yangjibao import YangJiBaoConnector
from app.models.connector import Connector, PersonalConnectorBinding
from app.models.identity import Workspace
from app.services.personal_credentials import read_credentials
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider
from tests.test_chat_integration import chat as chat


@pytest.mark.integration
async def test_members_can_scan_yangjibao_independently(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """两个成员同时扫码，各自确认、读取和撤销，密文中不含令牌。"""
    app, admin, _ = chat
    factory = app.state.database.session_factory
    settings = app.state.settings
    monkeypatch.setattr(settings, "connector_credential_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(YangJiBaoConnector, "require_scan_ready", lambda self: None)
    request = AsyncMock(side_effect=[
        {"id": "one", "url": "https://weixin.qq.com/qr/one"},
        {"id": "two", "url": "https://weixin.qq.com/qr/two"},
        {"state": 2, "token": "private-token-two"},
        {"state": 2, "token": "private-token-one"},
    ])
    monkeypatch.setattr(yangjibao_authorization, "_request", request)
    owner = app.state.test_scope
    path = "/api/connectors/yangjibao/my-account"
    try:
        assert (await admin.post("/api/admin/connectors/yangjibao/install")).is_success
        assert (await admin.post("/api/admin/connectors/yangjibao/enabled", json={"enabled": True})).is_success
        async with factory() as session:
            slug = await session.scalar(select(Workspace.slug).where(Workspace.id == owner.workspace_id))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as member:
            scope = await add_identity(app, member, workspace=slug)
            first = await admin.post(path + "/auth")
            second = await member.post(path + "/auth")
            assert first.is_success and second.is_success
            assert first.json()["qr_content"] != second.json()["qr_content"]
            assert first.headers["cache-control"] == "no-store"
            assert (await admin.post(path + "/auth")).json() == first.json()
            assert (await member.get(path + "/auth/status")).json()["qr_content"] == second.json()["qr_content"]
            assert request.await_count == 2
            assert (await member.post(path + "/auth/status")).json()["state"] == "completed"
            assert not (await admin.get("/api/connectors/yangjibao")).json()["personal_account"]["bound"]
            assert (await admin.post(path + "/auth/status")).json()["state"] == "completed"
            async with factory() as session:
                assert (await read_credentials(session, settings, owner, "yangjibao"))["token"] == "private-token-one"
                assert (await read_credentials(session, settings, scope, "yangjibao"))["token"] == "private-token-two"
                rows = list(await session.scalars(select(PersonalConnectorBinding.ciphertext)))
                assert all("private-token" not in value for value in rows)
            assert (await member.get("/api/connectors/yangjibao")).json()["can_view_holdings"]
            assert (await member.delete(path)).json()["bound"] is False
            assert (await member.post(path + "/auth/status")).json()["state"] == "failed"
            assert (await admin.get("/api/connectors/yangjibao")).json()["personal_account"]["bound"]
            assert (await member.put(path, json={"credentials": {"token": "injected"}})).status_code == 409
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "yangjibao"))
        await app.state.connector_registry.refresh("yangjibao")


class FakeAuthorization:
    """只替代外部扫码进程，保留实际会话隔离和数据库写入。"""

    instances: list["FakeAuthorization"] = []

    def __init__(
        self, cli_path: str, on_finished: Callable[[], Awaitable[None]], *, env: dict[str, str], cwd: Path,
    ) -> None:
        """记录传给真实 CLI 的个人目录。"""
        self.callback, self.env, self.cwd = on_finished, env, cwd
        self.state = "awaiting_user"
        self.instances.append(self)

    async def start(self) -> dict[str, object]:
        """直接返回安全的测试授权入口。"""
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        """每个流程有自己的授权码。"""
        return {"state": self.state, "auth_url": "https://meeting.tencent.com/?code=" + self.cwd.name,
                "output": "等待扫码", "already_logged_in": False, "expires_at": None}

    async def close(self) -> None:
        """模拟关闭当前流程。"""
        self.state = "failed"


@pytest.mark.integration
async def test_member_meeting_authorization_uses_separate_directories_and_revokes_late_callback(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """个人 CLI 登录和调用同目录，解绑后的迟到回调不能恢复绑定。"""
    app, admin, _ = chat
    factory = app.state.database.session_factory
    registry = app.state.connector_registry
    settings = app.state.settings
    monkeypatch.setattr(settings, "connector_credential_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(settings, "personal_connector_data_dir", tmp_path)
    monkeypatch.setattr(personal_authorization, "CliAuthorization", FakeAuthorization)
    monkeypatch.setattr(CliConnector, "check_authentication", AsyncMock())
    FakeAuthorization.instances = []
    owner = app.state.test_scope
    path = "/api/connectors/tmeet/my-account"
    try:
        assert (await admin.post("/api/admin/connectors/tmeet/install")).is_success
        assert (await admin.post("/api/admin/connectors/tmeet/enabled", json={"enabled": True})).is_success
        async with factory() as session:
            slug = await session.scalar(select(Workspace.slug).where(Workspace.id == owner.workspace_id))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as member:
            scope = await add_identity(app, member, workspace=slug)
            assert (await member.get(path + "/auth/status")).json()["state"] == "failed"
            assert (await admin.post(path + "/auth")).is_success
            assert (await member.post(path + "/auth")).is_success
            first, second = FakeAuthorization.instances
            assert first.cwd != second.cwd and str(scope.user_id) in str(second.cwd)
            for variable in ("TMEET_CLI_CONFIG_DIR", "TMEET_CLI_DATA_DIR", "HOME", "USERPROFILE"):
                assert first.env[variable] != second.env[variable]
            await first.callback()
            await second.callback()
            first.state = second.state = "completed"
            async with factory() as session:
                credentials = await read_credentials(session, settings, scope, "tmeet")
            connector: Any = registry.resolve("tmeet")
            runner = connector.meeting(scope, credentials["profile_id"])._runner
            assert runner._cwd == second.cwd and runner._env == second.env
            assert (await member.get(path + "/auth/status")).json()["auth_url"] != first.snapshot()["auth_url"]
            assert (await member.delete(path)).json()["bound"] is False
            await second.callback()
            assert (await member.get("/api/connectors/tmeet")).json()["personal_account"]["bound"] is False
            assert (await admin.get("/api/connectors/tmeet")).json()["personal_account"]["bound"]
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "tmeet"))
        await registry.refresh("tmeet")


@pytest.mark.integration
async def test_unbind_waits_for_pending_qr_and_disabled_plugin_cannot_issue_more(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取码等待时解绑也能关闭新会话，停用后不再请求上游二维码。"""
    app, client, _ = chat
    factory = app.state.database.session_factory
    registry = app.state.connector_registry
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(YangJiBaoConnector, "require_scan_ready", lambda self: None)

    async def qr(path: str) -> dict[str, object]:
        """模拟上游取码慢于用户点击解绑。"""
        started.set()
        await release.wait()
        return {"id": "pending", "url": "https://weixin.qq.com/qr/pending"}

    request = AsyncMock(side_effect=qr)
    monkeypatch.setattr(yangjibao_authorization, "_request", request)
    path = "/api/connectors/yangjibao/my-account"
    tasks: list[asyncio.Task[httpx.Response]] = []
    try:
        await client.post("/api/admin/connectors/yangjibao/install")
        await client.post("/api/admin/connectors/yangjibao/enabled", json={"enabled": True})
        tasks.append(asyncio.create_task(client.post(path + "/auth")))
        await asyncio.wait_for(started.wait(), 2)
        tasks.append(asyncio.create_task(client.delete(path)))
        await asyncio.sleep(0)
        release.set()
        issued, removed = await asyncio.gather(*tasks)
        assert issued.is_success and removed.is_success
        assert removed.json()["bound"] is False
        assert (await client.get(path + "/auth/status")).json()["state"] == "failed"
        await client.post("/api/admin/connectors/yangjibao/enabled", json={"enabled": False})
        assert (await client.post(path + "/auth")).status_code == 409
        assert request.await_count == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "yangjibao"))
        await registry.refresh("yangjibao")
