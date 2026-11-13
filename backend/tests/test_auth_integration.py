"""真实会话、CSRF、资源越权与动态撤权集成验证。"""

import asyncio
import socket
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import ApplicationError
from app.core.security import COOKIE_NAME, csrf_token, digest_token
from app.models.conversation import Conversation, Message
from app.models.identity import AuthSession, User, Workspace, WorkspaceMembership
from app.services.accounts import AccountManager
from tests.auth_helpers import TEST_PASSWORD, add_identity, copied_headers
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration


async def test_session_cookie_rotation_logout_and_csrf(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """会话可恢复、轮换和撤销，写入要求可信来源与会话绑定 CSRF。

    Args:
        chat: 真实登录后的应用。
    """
    app, client, _ = chat
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert set(me.json()["user"]) == {"id", "username", "display_name"}
    original = client.cookies.get(COOKIE_NAME)
    assert original and me.json()["csrf_token"] == csrf_token(original)
    async with app.state.database.session_factory() as session:
        stored = await session.scalar(
            select(AuthSession.token_digest).where(AuthSession.user_id == app.state.test_scope.user_id)
        )
        assert stored == digest_token(original) and stored != original
    for headers in ({"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}):
        assert (await client.post("/api/conversations", json={}, headers=headers)).status_code == 403
    response = await client.post(
        "/api/auth/login", json={"username": me.json()["user"]["username"], "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"] and "Path=/api" in response.headers["set-cookie"]
    assert "password" not in response.text and original not in response.text
    assert client.cookies.get(COOKIE_NAME) != original
    with pytest.raises(ApplicationError):
        await app.state.auth_service.authenticate(original)
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await client.post("/api/auth/logout")).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.post("/api/auth/logout")).status_code == 204


async def test_all_old_chat_routes_require_login(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """旧路由没有匿名兼容后门，知道 UUID 仍不能访问。

    Args:
        chat: 测试应用。
    """
    app, client, _ = chat
    conversation = await new_conversation(client)
    run = await submit(client, conversation)
    await settled(client, run["run"]["id"])
    run_id = run["run"]["id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://anonymous") as anonymous:
        paths = [
            ("GET", "/api/conversations"),
            ("POST", "/api/conversations"),
            ("GET", f"/api/conversations/{conversation}/messages"),
            ("POST", f"/api/conversations/{conversation}/runs"),
            ("GET", f"/api/runs/{run_id}"),
            ("POST", f"/api/runs/{run_id}/cancel"),
            ("GET", f"/api/runs/{run_id}/events"),
        ]
        for method, path in paths:
            body = {"content": "x", "idempotency_key": "x"} if path.endswith("/runs") else {}
            response = await anonymous.request(method, path, json=body if method == "POST" else None)
            assert response.status_code == 401, (path, response.text)
        assert (await anonymous.get("/api/health/live")).status_code == 200


@pytest.mark.parametrize("role", ["member", "admin"])
async def test_other_users_cannot_read_generate_cancel_or_subscribe(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider], role: str
) -> None:
    """成员与管理员均不能访问他人的私有对话，包括运行和摘要路径。

    Args:
        chat: 测试应用。
        role: 攻击方的空间角色。
    """
    app, client, provider = chat
    provider.mode = "wait"
    conversation = await new_conversation(client)
    created = await submit(client, conversation)
    run_id = created["run"]["id"]
    await asyncio.wait_for(provider.received.wait(), 5)
    me = (await client.get("/api/auth/me")).json()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await add_identity(app, other, me["workspaces"][0]["slug"], role=role)
        assert (await other.get("/api/conversations")).json() == []
        paths = [
            ("GET", f"/api/conversations/{conversation}/messages"),
            ("POST", f"/api/conversations/{conversation}/runs"),
            ("GET", f"/api/runs/{run_id}"),
            ("POST", f"/api/runs/{run_id}/cancel"),
            ("GET", f"/api/runs/{run_id}/events"),
        ]
        for method, path in paths:
            response = await other.request(
                method, path, json={"content": "越权", "idempotency_key": "attack"} if method == "POST" else None
            )
            assert response.status_code == 404, response.text
        assert (await client.get(f"/api/runs/{run_id}")).json()["run"]["status"] == "running"
    await client.post(f"/api/runs/{run_id}/cancel")


async def test_workspace_selection_is_not_authority(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """修改空间参数不能取得非成员权限，同属多个空间也不能串读资源。

    Args:
        chat: 测试应用。
    """
    app, client, _ = chat
    conversation = await new_conversation(client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        second = await add_identity(app, other)
        client.headers["X-Workspace-ID"] = str(second.workspace_id)
        assert (await client.get("/api/conversations")).status_code == 403
        async with app.state.database.session_factory.begin() as session:
            session.add(WorkspaceMembership(workspace_id=second.workspace_id, user_id=app.state.test_scope.user_id))
        assert (await client.get("/api/conversations")).json() == []
        assert (await client.get(f"/api/conversations/{conversation}/messages")).status_code == 404
        foreign = await new_conversation(other)
        assert (await client.get(f"/api/conversations/{foreign}/messages")).status_code == 404


@pytest.mark.parametrize("reason", ["expired", "disabled", "version"])
async def test_session_expiry_disable_and_version(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider], reason: str
) -> None:
    """过期、禁用和版本变化立即拒绝旧 Cookie。

    Args:
        chat: 测试应用。
        reason: 会话失效原因。
    """
    app, client, _ = chat
    async with app.state.database.session_factory.begin() as session:
        if reason == "expired":
            now = datetime.now(UTC)
            await session.execute(
                update(AuthSession)
                .where(AuthSession.user_id == app.state.test_scope.user_id)
                .values(created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1))
            )
        elif reason == "disabled":
            await session.execute(update(User).where(User.id == app.state.test_scope.user_id).values(status="disabled"))
        else:
            await session.execute(update(User).where(User.id == app.state.test_scope.user_id).values(session_version=2))
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.get("/api/conversations")).status_code == 401


async def test_login_failure_window_and_origin(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """错误口令与未知账号统一响应，登录窗口持久限制且检查来源。

    Args:
        chat: 测试应用。
    """
    app, client, _ = chat
    username = (await client.get("/api/auth/me")).json()["user"]["username"]
    app.state.settings.auth_login_max_failures = 2
    assert (
        await client.post(
            "/api/auth/login", json={"username": username, "password": TEST_PASSWORD}, headers={"Origin": "null"}
        )
    ).status_code == 403
    for _ in range(2):
        response = await client.post("/api/auth/login", json={"username": username, "password": "wrong"})
        assert response.status_code == 401 and response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert (
        await client.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    ).status_code == 429
    unknown = await client.post("/api/auth/login", json={"username": "missing_" + uuid4().hex, "password": "wrong"})
    assert unknown.status_code == 401 and unknown.json()["error"]["message"] == "账号或密码错误"


async def test_password_reset_revokes_sessions(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """重置密码使旧会话和旧密码失效，不修改应用数据。

    Args:
        chat: 测试应用。
    """
    app, client, _ = chat
    username = (await client.get("/api/auth/me")).json()["user"]["username"]
    await AccountManager(app.state.database.session_factory).reset_password(username, "new-test-password-123!")
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (
        await client.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    ).status_code == 401
    assert (
        await client.post("/api/auth/login", json={"username": username, "password": "new-test-password-123!"})
    ).status_code == 200


async def test_revocation_stops_generation_without_new_tokens(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """供应商没有新片段时，成员撤销仍触发运行终止和访问拒绝。

    Args:
        chat: 测试应用。
    """
    app, client, provider = chat
    provider.mode = "wait"
    conversation = await new_conversation(client)
    created = await submit(client, conversation)
    await asyncio.wait_for(provider.received.wait(), 5)
    async with app.state.database.session_factory.begin() as session:
        await session.execute(
            update(WorkspaceMembership)
            .where(WorkspaceMembership.user_id == app.state.test_scope.user_id)
            .values(status="revoked")
        )
    async with asyncio.timeout(5):
        while True:
            snapshot = await app.state.chat_runtime.store.snapshot(UUID(created["run"]["id"]))
            if snapshot.run.status not in ("running", "pending"):
                break
            await asyncio.sleep(0.05)
    assert snapshot.run.error_code == "ACCESS_REVOKED" and snapshot.message.status == "interrupted"
    assert snapshot.message.content
    assert (await client.get(f"/api/runs/{snapshot.run.id}")).status_code == 403


async def test_legacy_claim_is_explicit_and_preserves_content(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """无归属记录默认隐藏，显式认领只填归属且可重复执行。

    Args:
        chat: 测试应用。
    """
    app, client, _ = chat
    legacy_id = uuid4()
    async with app.state.database.session_factory.begin() as session:
        session.add(Conversation(id=legacy_id, title="旧对话"))
        await session.flush()
        session.add(
            Message(conversation_id=legacy_id, sequence=1, role="user", content="保留原问题", status="completed")
        )
    assert (await client.get(f"/api/conversations/{legacy_id}/messages")).status_code == 404
    me = (await client.get("/api/auth/me")).json()
    manager = AccountManager(app.state.database.session_factory)
    assert await manager.claim(me["user"]["username"], me["workspaces"][0]["slug"]) >= 1
    assert await manager.claim(me["user"]["username"], me["workspaces"][0]["slug"]) == 0
    assert (await client.get(f"/api/conversations/{legacy_id}/messages")).json()["items"][0]["content"] == "保留原问题"


async def test_archived_workspace_read_cancel_but_no_write(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """归档空间能读已有私有历史，不能再创建或生成。

    Args:
        chat: 测试应用。
    """
    app, client, _ = chat
    conversation = await new_conversation(client)
    async with app.state.database.session_factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == app.state.test_scope.workspace_id).values(status="archived")
        )
    assert (await client.get(f"/api/conversations/{conversation}/messages")).status_code == 200
    assert (await client.post("/api/conversations", json={})).status_code == 409
    assert (
        await client.post(f"/api/conversations/{conversation}/runs", json={"content": "x", "idempotency_key": "x"})
    ).status_code == 409


@pytest.mark.parametrize("reason", ["logout", "member", "expired"])
async def test_live_sse_revalidates_access(chat: tuple[FastAPI, AsyncClient, ControlledProvider], reason: str) -> None:
    """真实 TCP 连接在退出、撤权或过期后停止发送私有正文。

    Args:
        chat: 已登录应用。
        reason: 连接建立后的失效原因。
    """
    app, client, provider = chat
    provider.mode = "wait"
    conversation = await new_conversation(client)
    created = await submit(client, conversation)
    run_id = created["run"]["id"]
    await asyncio.wait_for(provider.received.wait(), 5)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    ready = asyncio.Event()

    class NotifyingServer(uvicorn.Server):
        """以事件同步测试服务器启动。"""

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            """启动监听并通知客户端。

            Args:
                sockets: 本机随机端口监听。
            """
            await super().startup(sockets)
            ready.set()

    server = NotifyingServer(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=copied_headers(client), timeout=5
        ) as network:
            async with network.stream("GET", f"/api/runs/{run_id}/events") as stream:
                assert stream.status_code == 200
                lines = stream.aiter_lines()
                async for line in lines:
                    if line.startswith("data:"):
                        assert "部分回答" in line
                        break
                if reason == "logout":
                    assert (await client.post("/api/auth/logout")).status_code == 204
                else:
                    async with app.state.database.session_factory.begin() as session:
                        if reason == "member":
                            await session.execute(
                                update(WorkspaceMembership)
                                .where(WorkspaceMembership.user_id == app.state.test_scope.user_id)
                                .values(status="revoked")
                            )
                        else:
                            now = datetime.now(UTC)
                            await session.execute(
                                update(AuthSession)
                                .where(AuthSession.user_id == app.state.test_scope.user_id)
                                .values(created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1))
                            )
                remaining = [line async for line in lines]
                assert "event: access_revoked" in remaining
                assert not any("部分回答" in line for line in remaining)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def test_spoofed_owner_and_partial_ownership_rejected(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """前端提交的所有者不会成为可信身份，数据库也拒绝半归属记录。

    Args:
        chat: 已登录应用。
    """
    app, client, _ = chat
    response = await client.post(
        "/api/conversations",
        json={"title": "归属验证", "created_by_user_id": str(uuid4()), "workspace_id": str(uuid4())},
    )
    assert response.status_code == 201
    async with app.state.database.session_factory.begin() as session:
        conversation = await session.get(Conversation, UUID(response.json()["id"]))
        assert conversation.created_by_user_id == app.state.test_scope.user_id
        assert conversation.workspace_id == app.state.test_scope.workspace_id
        for values in (
            {"workspace_id": app.state.test_scope.workspace_id},
            {"workspace_id": uuid4(), "created_by_user_id": app.state.test_scope.user_id},
        ):
            async with session.begin_nested() as savepoint:
                with pytest.raises(IntegrityError):
                    await session.execute(insert(Conversation).values(title="非法归属", **values))
                await savepoint.rollback()


async def test_account_reinitialization_does_not_reset_password(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """重复初始化不得悄悄重置已有账号，也不能向不存在空间添加账号。

    Args:
        chat: 已登录应用。
    """
    app, client, _ = chat
    me = (await client.get("/api/auth/me")).json()
    manager = AccountManager(app.state.database.session_factory)
    with pytest.raises(ApplicationError) as repeated:
        await manager.create(me["user"]["username"], "different-password-123!", "重复", initialize=True)
    assert repeated.value.code == "ACCOUNT_EXISTS"
    with pytest.raises(ApplicationError) as missing:
        await manager.create("new_" + uuid4().hex, TEST_PASSWORD, "无空间", slug="missing-space")
    assert missing.value.code == "WORKSPACE_NOT_FOUND"
    assert (await client.get("/api/auth/me")).status_code == 200
    username = me["user"]["username"]
    response = await client.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 200
