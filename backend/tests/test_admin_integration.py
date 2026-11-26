"""空间管理员管理成员与角色的真实 PostgreSQL 验收。"""

import asyncio
from base64 import b64decode
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.models.admin_audit import AdminAudit
from app.models.config import IntegrationConfig
from app.models.identity import User, Workspace, WorkspaceMembership
from app.services import config_probe
from app.services.config_service import (
    ACTIVE_PROFILE_KEY,
    ENCRYPTED_PREFIX,
    MODEL_CONNECTION_KEYS,
    PROFILE_PREFIX,
    ConfigService,
)
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration


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


async def test_api_settings_probe_uses_temporary_credentials(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """串起真实 API 鉴权、临时配置合并和脱敏结果，不调用付费供应商。"""
    app, client, _ = chat
    settings: Settings = app.state.settings
    path = "/api/admin/configs/test"
    assert (await client.post(path, json={"group": "llm"})).status_code == 403
    settings.system_admin_username = (await client.get("/api/auth/me")).json()["user"]["username"]
    settings.llm_provider = "openai_compatible"
    settings.llm_base_url = "https://configured.example.invalid/v1"
    settings.llm_model = "old-text-model"
    settings.llm_vision_model = "saved-image-model"
    settings.llm_api_key = SecretStr("stored-model-key")
    settings.tavily_api_key = SecretStr("stored-search-key")
    calls: list[tuple[str, str, bool]] = []

    class ProbeModel:
        """验证探针使用正确的调用形态和可解码的内置图片。"""

        def __init__(self, vision: bool) -> None:
            """记录是文本还是图文调用。"""
            self.vision = vision

        async def ainvoke(self, messages: list[HumanMessage]) -> AIMessage:
            """返回文本成功和带敏感正文的图文拒绝。"""
            content = messages[0].content
            if self.vision:
                assert isinstance(content, list) and isinstance(content[1], dict)
                url = content[1]["image_url"]["url"]
                with Image.open(BytesIO(b64decode(url.split(",", 1)[1], validate=True))) as image:
                    assert image.size == (64, 64)
                    image.verify()
                request = Request("POST", "https://configured.example.invalid/v1/chat/completions")
                raise HTTPStatusError("upstream echoed stored-model-key", request=request, response=Response(403))
            assert isinstance(content, str)
            return AIMessage(content="OK")

    def model(candidate: Settings, *, max_tokens: int, vision: bool) -> ProbeModel:
        """记录发给模型客户端的实际配置快照。"""
        assert max_tokens <= config_probe.PROBE_TOKENS
        model_id = candidate.llm_vision_model if vision else candidate.llm_model
        calls.append((model_id, candidate.llm_api_key.get_secret_value(), vision))
        return ProbeModel(vision)

    async def search(api_key: str, query: str, max_results: int, timeout_seconds: float) -> list[dict[str, str]]:
        """检查 Tavily 使用已保存密钥和一条搜索结果上限。"""
        assert api_key == "stored-search-key" and query and max_results == 1 and timeout_seconds > 0
        return []

    monkeypatch.setattr(config_probe, "create_llm", model)
    monkeypatch.setattr(config_probe, "search_tavily", search)
    del client.headers["X-Workspace-ID"]
    wrong_csrf = await client.post(path, json={"group": "llm"}, headers={"X-CSRF-Token": "wrong"})
    assert wrong_csrf.status_code == 403
    changed_url = await client.post(path, json={
        "group": "llm", "configs": [{"key": "llm_base_url", "value": "https://other.example.invalid/v1"}]
    })
    assert changed_url.status_code == 422 and changed_url.json()["error"]["code"] == "CONFIG_LLM_KEY_REQUIRED"
    assert not calls
    response = await client.post(path, json={
        "group": "llm", "configs": [{"key": "llm_model", "value": "draft-text-model"}]
    })
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert [(item["check"], item["ok"]) for item in response.json()] == [("text", True), ("vision", False)]
    assert "stored-model-key" not in response.text and "API Key" in response.json()[1]["message"]
    assert calls == [("draft-text-model", "stored-model-key", False), ("saved-image-model", "stored-model-key", True)]
    assert settings.llm_model == "old-text-model"
    assert (await client.post(path, json={"group": "search"})).json()[0]["ok"] is True
    assert (await client.post(path, json={
        "group": "search", "configs": [{"key": "tavily_api_key", "value": ""}]
    })).json()[0]["ok"] is False
    assert settings.tavily_api_key.get_secret_value() == "stored-search-key"


async def test_add_test_and_switch_saved_models(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持久化两份独立配置并切换，保证测试和添加期间当前模型保持可用。"""
    app, client, _ = chat
    settings: Settings = app.state.settings
    path = "/api/admin/model-profiles"
    assert (await client.get(path)).status_code == 403
    settings.system_admin_username = (await client.get("/api/auth/me")).json()["user"]["username"]
    settings.integration_config_key_file = tmp_path / "configured-key"
    original_model = settings.active_llm_model
    factory = app.state.database.session_factory
    keys = list(MODEL_CONNECTION_KEYS | {ACTIVE_PROFILE_KEY})
    created: list[str] = []
    async with factory() as session:
        before = (await session.scalars(select(IntegrationConfig).where(IntegrationConfig.key.in_(keys)))).all()

    async def probe(candidate: Settings, group: str) -> list[config_probe.ConfigCheck]:
        """测试新模型始终使用表单凭据，空值表示本模型无鉴权。"""
        assert group == "llm" and candidate.llm_model == "model-one"
        assert candidate.llm_api_key.get_secret_value() == ""
        return [config_probe.ConfigCheck(check="text", ok=True, message="调用成功")]

    monkeypatch.setattr("app.api.admin.probe_connections", probe)
    first = {
        "name": "本地文本模型", "provider": "openai_compatible", "model_id": "model-one",
        "base_url": "http://llm-one.invalid/v1", "api_key": "first-model-key",
    }
    second = {"name": "团队图片模型", "provider": "openai_compatible", "model_id": "model-two",
              "base_url": "https://llm-two.invalid/v1", "api_key": "second-model-key", "vision_model_id": "vision-two"}
    try:
        invalid = await client.post(path, json={**first, "base_url": "https://user:pass@provider.invalid/v1"})
        assert invalid.status_code == 422
        for payload in (first, second):
            response = await client.post(path, json=payload)
            assert response.status_code == 201, response.text
            assert response.headers["Cache-Control"] == "no-store"
            assert payload["api_key"] not in response.text
            item = response.json()
            assert item["has_api_key"] and not item["active"]
            created.append(item["id"])
        assert settings.active_llm_model == original_model
        probe_result = await client.post(f"{path}/test", json={**first, "api_key": ""})
        assert probe_result.status_code == 200 and probe_result.json()[0]["ok"] is True
        assert settings.active_llm_model == original_model
        for profile_id, payload in zip(created, (first, second), strict=True):
            response = await client.post(f"{path}/{profile_id}/activate")
            assert response.status_code == 200 and payload["api_key"] not in response.text
            assert settings.llm_api_key.get_secret_value() == payload["api_key"]
            assert settings.active_llm_model == payload["model_id"]
        profiles = await client.get(path)
        assert "second-model-key" not in profiles.text
        assert [item["id"] for item in profiles.json() if item["active"]] == [created[-1]]
        async with factory() as session:
            row = await session.get(IntegrationConfig, f"{PROFILE_PREFIX}{created[-1]}")
            assert row is not None and row.value.startswith(ENCRYPTED_PREFIX) and "second-model-key" not in row.value
        fresh = Settings(_env_file=None, integration_config_key_file=settings.integration_config_key_file)
        await ConfigService(factory, fresh).load()
        assert fresh.active_llm_model == "model-two" and fresh.llm_api_key.get_secret_value() == "second-model-key"
        changed = await client.post("/api/admin/configs", json={"configs": [{"key": "llm_model", "value": "manual"}]})
        assert changed.status_code == 200
        assert not any(item["active"] for item in (await client.get(path)).json())
        settings.integration_config_key_file = tmp_path / "missing-key"
        assert (await client.post(path, json=first)).status_code == 503
        assert not settings.integration_config_key_file.exists()
    finally:
        async with factory.begin() as session:
            await session.execute(delete(IntegrationConfig).where(IntegrationConfig.key.in_(
                [*keys, *(f"{PROFILE_PREFIX}{profile_id}" for profile_id in created)]
            )))
            for row in before:
                await session.merge(row)


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
