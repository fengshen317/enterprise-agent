"""空间管理员管理成员与角色的真实 PostgreSQL 验收。"""

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.models.admin_audit import AdminAudit
from app.models.identity import User, Workspace, WorkspaceMembership
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration


async def create_user(app: FastAPI, client: AsyncClient, role: str = "member", password: str = "1") -> dict:
    """通过真实管理接口创建并登记清理范围。

    Args:
        app: 测试应用。
        client: 已登录管理员。
        role: 固定角色。
        password: 测试密码，无长度限制。

    Returns:
        创建的安全成员视图。
    """
    response = await client.post(
        "/api/admin/members",
        json={
            "username": "adminqa_" + uuid4().hex,
            "display_name": "后台测试成员",
            "password": password,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    app.state.test_users.append(UUID(body["user_id"]))
    return dict(body)


async def sign_in(client: AsyncClient, source: AsyncClient, username: str, password: str) -> None:
    """使用新账号真实登录，不绕过会话校验。

    Args:
        client: 新客户端。
        source: 提供空间和来源的管理员客户端。
        username: 测试账号。
        password: 测试密码。
    """
    client.headers["Origin"] = source.headers["Origin"]
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    client.headers["X-Workspace-ID"] = source.headers["X-Workspace-ID"]


async def test_admin_creates_users_with_real_login_and_safe_audit(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """新用户能登录，查询/搜索/审计不返回密码或哈希。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, _ = chat
    created = await create_user(app, client)
    assert created["role"] == "member" and created["status"] == "active"
    page = await client.get("/api/admin/members?limit=1")
    assert page.status_code == 200 and page.json()["total"] == 2 and len(page.json()["items"]) == 1
    result = await client.get("/api/admin/members", params={"q": created["username"]})
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["user_id"] == created["user_id"]
    assert "password" not in result.text and "$argon2" not in result.text
    assert (await client.get("/api/admin/members", params={"q": "%"})).json()["total"] == 0
    records = await client.get("/api/admin/audits")
    assert records.json()[0]["action"] == "create_user"
    assert "password" not in records.text and "$argon2" not in records.text
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await sign_in(member, client, created["username"], "1")
        assert (await member.post("/api/conversations", json={})).status_code == 201
        assert (await member.get("/api/admin/members")).status_code == 403
        assert (await member.get("/api/admin/audits")).status_code == 403
        assert (
            await member.post(
                "/api/admin/members", json={"username": "cannot_create", "display_name": "无权", "password": "1"}
            )
        ).status_code == 403
        assert (
            await member.post(f"/api/admin/members/{created['user_id']}", json={"role": "admin", "status": "active"})
        ).status_code == 403
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://anonymous") as anonymous:
        assert (await anonymous.get("/api/admin/members")).status_code == 401


async def test_roles_revoke_restore_and_other_space_boundaries(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """角色即时生效，停用只影响当前空间，不修改全局账号。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, provider = chat
    created = await create_user(app, client)
    path = f"/api/admin/members/{created['user_id']}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await sign_in(member, client, created["username"], "1")
        assert (await client.post(path, json={"role": "admin", "status": "active"})).status_code == 200
        assert (await member.get("/api/admin/members")).status_code == 200
        assert (await client.post(path, json={"role": "member", "status": "active"})).status_code == 200
        assert (await member.get("/api/admin/members")).status_code == 403
        provider.mode = "wait"
        run = await submit(member, await new_conversation(member))
        await provider.received.wait()
        assert (await client.post(path, json={"role": "member", "status": "revoked"})).status_code == 200
        assert (await member.get("/api/conversations")).status_code == 403
        async with app.state.database.session_factory() as session:
            user = await session.get(User, UUID(created["user_id"]))
            assert user is not None and user.status == "active"
        # 后台撤权监视器应终止运行；管理者不能借此读取其正文。
        task = app.state.chat_runtime.tasks.get(UUID(run["run"]["id"]))
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert (await client.get(f"/api/runs/{run['run']['id']}")).status_code == 404
        assert (await client.post(path, json={"role": "member", "status": "active"})).status_code == 200
        assert (await member.get("/api/conversations")).status_code == 200
        terminal = await settled(member, run["run"]["id"])
        assert terminal["run"]["error_code"] == "ACCESS_REVOKED"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as outsider:
        other = await add_identity(app, outsider)
        assert (await outsider.post(path, json={"role": "admin", "status": "active"})).status_code == 404
        response = await client.get("/api/admin/members", headers={"X-Workspace-ID": str(other.workspace_id)})
        assert response.status_code == 403
        assert (await outsider.get("/api/admin/audits")).json() == []


async def test_last_admin_and_concurrent_self_demotion(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """末位管理员不能被移除；两个管理员并发降级仍保留一人。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, _ = chat
    owner = app.state.test_scope.user_id
    path = f"/api/admin/members/{owner}"
    for body in ({"role": "member", "status": "active"}, {"role": "admin", "status": "revoked"}):
        response = await client.post(path, json=body)
        assert response.status_code == 409 and response.json()["error"]["code"] == "LAST_ADMIN_REQUIRED"
    second = await create_user(app, client, role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await sign_in(other, client, second["username"], "1")
        results = await asyncio.gather(
            client.post(path, json={"role": "member", "status": "active"}),
            other.post(f"/api/admin/members/{second['user_id']}", json={"role": "member", "status": "active"}),
        )
        assert sorted(response.status_code for response in results) == [200, 409]
    async with app.state.database.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == app.state.test_scope.workspace_id,
                WorkspaceMembership.role == "admin",
                WorkspaceMembership.status == "active",
            )
        )
        assert count == 1


async def test_management_rejects_csrf_forged_fields_and_duplicate_user(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """创建幂等边界不覆盖密码，拒绝跨空间字段与越界角色。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, _ = chat
    created = await create_user(app, client, password="")
    body = {"username": created["username"], "display_name": "新名字", "password": "changed", "role": "admin"}
    assert (await client.post("/api/admin/members", json=body)).status_code == 409
    for headers in ({"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}):
        assert (await client.post("/api/admin/members", json=body, headers=headers)).status_code == 403
    for extra in ({"workspace_id": str(uuid4())}, {"user_id": str(uuid4())}, {"role": "superadmin"}):
        assert (await client.post("/api/admin/members", json={**body, **extra})).status_code == 422
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await sign_in(member, client, created["username"], "")
    async with app.state.database.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AdminAudit)
            .where(AdminAudit.workspace_id == app.state.test_scope.workspace_id)
        )
        assert count == 1


async def test_archive_and_demotion_reject_late_write(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """管理角色撤销或空间归档后，旧会话不能继续管理。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, _ = chat
    second = await create_user(app, client, role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await sign_in(other, client, second["username"], "1")
        assert (
            await client.post(f"/api/admin/members/{second['user_id']}", json={"role": "member", "status": "active"})
        ).status_code == 200
        assert (await other.get("/api/admin/members")).status_code == 403
    async with app.state.database.session_factory.begin() as session:
        workspace = await session.get(Workspace, app.state.test_scope.workspace_id)
        assert workspace is not None
        workspace.status = "archived"
    assert (await client.get("/api/admin/members")).status_code == 403
    assert (
        await client.post(
            "/api/admin/members", json={"username": "archive_denied", "display_name": "无权", "password": "1"}
        )
    ).status_code == 409


async def test_revoking_one_space_preserves_other_space_access(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """空间管理员不能通过停用成员禁用全局账号。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, _ = chat
    created = await create_user(app, client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other_admin:
        other_scope = await add_identity(app, other_admin)
    async with app.state.database.session_factory.begin() as session:
        session.add(WorkspaceMembership(workspace_id=other_scope.workspace_id, user_id=UUID(created["user_id"])))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await sign_in(member, client, created["username"], "1")
        response = await client.post(
            f"/api/admin/members/{created['user_id']}", json={"role": "member", "status": "revoked"}
        )
        assert response.status_code == 200
        assert (await member.get("/api/conversations")).status_code == 403
        assert (
            await member.get("/api/conversations", headers={"X-Workspace-ID": str(other_scope.workspace_id)})
        ).status_code == 200


async def test_concurrent_create_has_one_user_and_one_audit(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """重复创建请求不会覆盖原账号或产生半份审计。

    Args:
        chat: 已登录管理员应用。
    """
    app, client, _ = chat
    body = {"username": "repeat_" + uuid4().hex, "display_name": "重复测试", "password": "1"}
    responses = await asyncio.gather(
        client.post("/api/admin/members", json=body), client.post("/api/admin/members", json=body)
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    created = next(response.json() for response in responses if response.status_code == 201)
    app.state.test_users.append(UUID(created["user_id"]))
    rows = (await client.get("/api/admin/audits")).json()
    assert len(rows) == 1 and rows[0]["target_username"] == body["username"]
