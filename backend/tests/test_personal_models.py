"""真实账号验证个人模型隔离与运行快照，外部模型使用受控传输。"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.agents.provider import DeepSeekProvider
from app.models.config import IntegrationConfig
from app.models.conversation import Run
from app.schemas.chat import RunCreate
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation, settled
from tests.test_chat_integration import chat as chat

BASE = "/api/me/models"
DRAFT = {
    "name": "我的 MiMo", "provider": "openai_compatible", "base_url": "https://api.xiaomimimo.com/v1",
    "model_id": "fixture-personal", "api_key": "personal-test-secret", "make_default": True,
}


async def test_personal_models_ownership_and_run_snapshot(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成员保存与切换仅影响本人，已提交运行在切换后继续使用原配置。"""
    app, client, _ = chat
    settings = app.state.settings
    settings.integration_config_key_file = tmp_path / "personal-model.key"
    config = app.state.config_service
    captured: list[tuple[str, str]] = []

    async def stream(
        self: DeepSeekProvider, history: list[tuple[str, str]], *, images: tuple[tuple[str, bytes], ...] = (),
        on_preview: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        """观察真实运行入口传入的模型和凭据，不请求收费模型。"""
        captured.append((self.settings.llm_model, self.settings.llm_api_key.get_secret_value()))
        yield "个人模型已执行"

    monkeypatch.setattr(DeepSeekProvider, "stream", stream)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://member") as member:
            owner = await add_identity(app, member)
            assert (await member.get("/api/admin/model-profiles")).status_code == 403
            created = await member.post(BASE, json=DRAFT)
            assert created.status_code == 201, created.text
            assert created.headers["cache-control"] == "no-store"
            assert DRAFT["api_key"] not in created.text
            profile_id = created.json()["profiles"][0]["id"]
            assert created.json()["profiles"][0]["active"]
            assert (await client.get(BASE)).json()["profiles"] == []
            assert (await client.post(f"{BASE}/{profile_id}/activate")).status_code == 404
            settings.system_admin_username = (await client.get("/api/auth/me")).json()["user"]["username"]
            assert (await client.post(f"/api/admin/model-profiles/{profile_id}/activate")).status_code == 404
            assert (await member.post(BASE, json={**DRAFT, "base_url": "http://127.0.0.1:8000"})).status_code == 422
            assert (await member.post(BASE, json=DRAFT, headers={"X-CSRF-Token": "wrong"})).status_code == 403
            app.state.chat_runtime.provider = DeepSeekProvider(settings)
            conversation = await new_conversation(member)
            snapshot = await app.state.chat_runtime.create_run(
                UUID(conversation), RunCreate(content="验证个人模型", idempotency_key="personal"), owner,
            )
            assert snapshot.run.model == DRAFT["model_id"]
            # 提交后恢复公共模型，当前轮仍必须使用提交时的个人凭据。
            await member.post(f"{BASE}/follow-shared")
            app.state.chat_runtime.schedule(snapshot.run.id, "personal-model-test")
            result = await settled(member, str(snapshot.run.id))
            assert result["run"]["status"] == "completed", result
            assert captured == [(DRAFT["model_id"], DRAFT["api_key"])]
            async with app.state.database.session_factory() as session:
                row = await session.get(Run, snapshot.run.id)
                assert row and row.model_connection and DRAFT["api_key"] not in row.model_connection
                stored = await session.scalar(select(IntegrationConfig.value).where(
                    IntegrationConfig.key == "personal-model:" + profile_id,
                ))
                assert stored and stored.startswith("fernet:v1:") and DRAFT["api_key"] not in stored
            assert (await config.settings_for_user(owner.user_id)).active_llm_model == settings.active_llm_model
            await member.post(f"{BASE}/{profile_id}/activate")
            settings.integration_config_key_file = tmp_path / "missing.key"
            unavailable = await member.get(BASE)
            assert unavailable.status_code == 503
            assert not settings.integration_config_key_file.exists()
    finally:
        async with app.state.database.session_factory.begin() as session:
            await session.execute(delete(IntegrationConfig).where(
                IntegrationConfig.updated_by.in_(app.state.test_users),
            ))
