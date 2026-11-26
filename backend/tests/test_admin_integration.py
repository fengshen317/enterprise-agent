"""空间管理员管理成员与角色的真实 PostgreSQL 验收。"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.core.security import hash_password
from app.models.admin_audit import AdminAudit
from app.models.config import IntegrationConfig
from app.models.identity import User, Workspace, WorkspaceMembership
from app.services.accounts import member_initial_password
from app.services.config_service import ENCRYPTED_PREFIX, ConfigService
from tests.auth_helpers import add_identity, copied_headers
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration
INITIAL_PASSWORD = "shared-fixture-password"
PERSONAL_PASSWORD = "personal-fixture-password"


async def test_generated_initial_password_is_shared_and_persisted(tmp_path: Path) -> None:
    """并发首次创建只保留一个初始密码，显式配置优先。"""
    secret_file = tmp_path / "member-initial-password"
    settings = Settings(_env_file=None, member_initial_password=SecretStr(""), member_initial_password_file=secret_file)
    assert member_initial_password(settings) == "" and not secret_file.exists()
    generated = await asyncio.gather(
        *(asyncio.to_thread(member_initial_password, settings, create=True) for _ in range(4))
    )
    assert len(set(generated)) == 1 and len(generated[0]) >= 8
    assert member_initial_password(settings) == secret_file.read_text(encoding="utf-8").strip() == generated[0]
    settings.member_initial_password = SecretStr(INITIAL_PASSWORD)
    assert member_initial_password(settings, create=True) == INITIAL_PASSWORD


async def test_deployment_api_settings(chat: tuple[FastAPI, AsyncClient, ControlledProvider], tmp_path: Path) -> None:
    """部署权限、批量保存、加密、重载和 Tavily 热启用使用真实数据库。"""
    app, client, _ = chat
    settings: Settings = app.state.settings
    settings.integration_config_key_file = tmp_path / "api-key"
    path = "/api/admin/configs"
    assert (await client.get(path)).status_code == 403
    settings.system_admin_username = (await client.get("/api/auth/me")).json()["user"]["username"]
    del client.headers["X-Workspace-ID"]
    initial = await client.get(path)
    assert initial.status_code == 200 and initial.headers["Cache-Control"] == "no-store"
    values = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "http://local-llm.invalid/v1",
        "llm_model": "fixture-model",
        "llm_vision_model": "fixture-vision",
        "llm_api_key": "fixture-llm-secret",
        "tavily_api_key": "fixture-search-secret",
        "model_max_tokens": "128",
    }
    factory = app.state.database.session_factory
    async with factory() as session:
        before = list(
            (await session.scalars(select(IntegrationConfig).where(IntegrationConfig.key.in_(tuple(values))))).all()
        )

    async def save(updates: dict[str, str]) -> Response:
        """将同一批配置交给真实 HTTP 接口。"""
        return await client.post(path, json={"configs": [{"key": key, "value": val} for key, val in updates.items()]})

    try:
        invalid = await save({**values, "model_max_tokens": "1000000"})
        assert invalid.status_code == 422 and settings.active_llm_model != "fixture-model"
        response = await save(values)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        assert "fixture-llm-secret" not in response.text and "fixture-search-secret" not in response.text
        assert settings.active_llm_model == "fixture-model"
        assert app.state.tool_registry.resolve("web_search") is not None
        async with factory() as session:
            saved = await session.get(IntegrationConfig, "llm_api_key")
            assert saved is not None and saved.value.startswith(ENCRYPTED_PREFIX)
            assert "fixture-llm-secret" not in saved.value
        fresh = Settings(_env_file=None, integration_config_key_file=settings.integration_config_key_file)
        await ConfigService(factory, fresh).load()
        assert fresh.llm_api_key.get_secret_value() == "fixture-llm-secret"
        assert fresh.llm_model == "fixture-model"
        changed = await save({"llm_base_url": "https://other-llm.invalid/v1"})
        assert changed.status_code == 422 and changed.json()["error"]["code"] == "CONFIG_LLM_KEY_REQUIRED"
        assert (await save({"tavily_api_key": ""})).status_code == 200
        assert app.state.tool_registry.resolve("web_search") is None
    finally:
        async with factory.begin() as session:
            await session.execute(delete(IntegrationConfig).where(IntegrationConfig.key.in_(tuple(values))))
            for row in before:
                await session.merge(row)


async def create_user(app: FastAPI, client: AsyncClient, role: str = "member") -> dict:
    """通过真实管理接口创建并登记清理范围。

    Args:
        app: 测试应用。
        client: 已登录管理员。
        role: 固定角色。

    Returns:
        创建的安全成员视图。
    """
    app.state.settings.member_initial_password = SecretStr(INITIAL_PASSWORD)
    response = await client.post(
        "/api/admin/members",
        json={
            "username": "adminqa_" + uuid4().hex,
            "display_name": "后台测试成员",
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


async def activate(client: AsyncClient, source: AsyncClient, username: str) -> None:
    """用初始密码登录并改密，再建立可执行业务操作的新会话。"""
    await sign_in(client, source, username, INITIAL_PASSWORD)
    response = await client.post(
        "/api/auth/change-password", json={"current_password": INITIAL_PASSWORD, "new_password": PERSONAL_PASSWORD}
    )
    assert response.status_code == 204, response.text
    await sign_in(client, source, username, PERSONAL_PASSWORD)


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
        await activate(member, client, created["username"])
        assert (await member.post("/api/conversations", json={})).status_code == 201
        assert (await member.get("/api/admin/members")).status_code == 403
        assert (await member.get("/api/admin/audits")).status_code == 403
        assert (
            await member.post("/api/admin/members", json={"username": "cannot_create", "display_name": "无权"})
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
        await activate(member, client, created["username"])
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
        assert (await outsider.post(f"{path}/reset-password")).status_code == 404
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
        await activate(other, client, second["username"])
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
    created = await create_user(app, client)
    body = {"username": created["username"], "display_name": "新名字", "role": "admin"}
    assert (await client.post("/api/admin/members", json=body)).status_code == 409
    for headers in ({"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}):
        assert (await client.post("/api/admin/members", json=body, headers=headers)).status_code == 403
    for extra in (
        {"workspace_id": str(uuid4())},
        {"user_id": str(uuid4())},
        {"role": "superadmin"},
        {"password": "changed"},
    ):
        assert (await client.post("/api/admin/members", json={**body, **extra})).status_code == 422
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await sign_in(member, client, created["username"], INITIAL_PASSWORD)
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
        await activate(other, client, second["username"])
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
        await client.post("/api/admin/members", json={"username": "archive_denied", "display_name": "无权"})
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
    reset_path = f"/api/admin/members/{created['user_id']}/reset-password"
    denied = await client.post(reset_path)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "PASSWORD_RESET_PROTECTED"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await activate(member, client, created["username"])
        response = await client.post(
            f"/api/admin/members/{created['user_id']}", json={"role": "member", "status": "revoked"}
        )
        assert response.status_code == 200
        assert (await client.post(reset_path)).json()["error"]["code"] == "PASSWORD_RESET_INACTIVE"
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
    app.state.settings.member_initial_password = SecretStr(INITIAL_PASSWORD)
    body = {"username": "repeat_" + uuid4().hex, "display_name": "重复测试"}
    responses = await asyncio.gather(
        client.post("/api/admin/members", json=body), client.post("/api/admin/members", json=body)
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    created = next(response.json() for response in responses if response.status_code == 201)
    app.state.test_users.append(UUID(created["user_id"]))
    rows = (await client.get("/api/admin/audits")).json()
    assert len(rows) == 1 and rows[0]["target_username"] == body["username"]


async def test_initial_password_change_reset_and_session_revocation(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """共用初始密码只能用于改密，改密和重置会立即注销已有会话。"""
    app, admin, _ = chat
    first = await create_user(app, admin)
    second = await create_user(app, admin)
    assert INITIAL_PASSWORD not in str(first) and INITIAL_PASSWORD not in str(second)
    factory = app.state.database.session_factory
    async with factory() as session:
        hashes = (
            await session.scalars(
                select(User.password_hash).where(User.id.in_([UUID(first["user_id"]), UUID(second["user_id"])]))
            )
        ).all()
        assert len(hashes) == 2 and len(set(hashes)) == 2

    path = "/api/auth/change-password"
    reset_path = f"/api/admin/members/{first['user_id']}/reset-password"
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as member,
        AsyncClient(transport=transport, base_url="http://test") as other_session,
    ):
        await sign_in(member, admin, first["username"], INITIAL_PASSWORD)
        await sign_in(other_session, admin, second["username"], INITIAL_PASSWORD)
        await sign_in(other_session, admin, first["username"], INITIAL_PASSWORD)
        session_view = (await member.get("/api/auth/me")).json()
        assert session_view["must_change_password"] is True and session_view["workspaces"] == []
        blocked = await member.get("/api/conversations")
        assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
        body = {"current_password": INITIAL_PASSWORD, "new_password": PERSONAL_PASSWORD}
        assert (await member.post(path, json=body, headers={"X-CSRF-Token": "wrong"})).status_code == 403
        assert (await member.post(path, json={**body, "current_password": "wrong"})).status_code == 422
        assert (await member.post(path, json={**body, "new_password": INITIAL_PASSWORD})).status_code == 422
        old_cookie = copied_headers(member)
        assert (await member.post(path, json=body)).status_code == 204
        assert (await member.get("/api/auth/me", headers=old_cookie)).status_code == 401
        assert (await other_session.get("/api/auth/me")).status_code == 401
        await sign_in(member, admin, first["username"], PERSONAL_PASSWORD)
        assert (await member.get("/api/auth/me")).json()["must_change_password"] is False
        assert (await member.get("/api/conversations")).status_code == 200
        assert (await member.post(reset_path)).status_code == 403
        assert (await admin.post(reset_path, headers={"Origin": "https://invalid.example"})).status_code == 403
        app.state.settings.system_admin_username = first["username"]
        assert (await admin.post(reset_path)).json()["error"]["code"] == "PASSWORD_RESET_PROTECTED"
        app.state.settings.system_admin_username = ""
        assert (await admin.post(reset_path)).status_code == 204
        assert (await member.get("/api/auth/me")).status_code == 401
        assert (
            await member.post("/api/auth/login", json={"username": first["username"], "password": PERSONAL_PASSWORD})
        ).status_code == 401
        await sign_in(member, admin, first["username"], INITIAL_PASSWORD)
        assert (await member.get("/api/auth/me")).json()["must_change_password"] is True
    audit = (await admin.get("/api/admin/audits")).json()[0]
    assert audit["action"] == "reset_password" and audit["target_username"] == first["username"]
    assert INITIAL_PASSWORD not in str(audit) and PERSONAL_PASSWORD not in str(audit)


async def test_existing_account_can_change_password_concurrently(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider], tmp_path: Path
) -> None:
    """兼容升级前的空密码，两个旧会话不能先后覆盖新密码。"""
    app, admin, _ = chat
    app.state.settings.member_initial_password_file = tmp_path / "unused-initial-password"
    username = (await admin.get("/api/auth/me")).json()["user"]["username"]
    async with app.state.database.session_factory.begin() as session:
        user = await session.get(User, app.state.test_scope.user_id)
        assert user is not None
        user.password_hash = await asyncio.to_thread(hash_password, "")
    other_password = "another-personal-fixture-password"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await sign_in(other, admin, username, "")
        path = "/api/auth/change-password"
        responses = await asyncio.gather(
            admin.post(path, json={"current_password": "", "new_password": PERSONAL_PASSWORD}),
            other.post(path, json={"current_password": "", "new_password": other_password}),
        )
        codes = [response.status_code for response in responses]
        assert codes.count(204) == 1 and all(code in {204, 401, 422} for code in codes)
        await sign_in(other, admin, username, PERSONAL_PASSWORD if codes[0] == 204 else other_password)
