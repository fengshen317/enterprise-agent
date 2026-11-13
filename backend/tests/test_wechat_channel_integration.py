"""真实 PostgreSQL 上的微信绑定与入站去重。iLink 使用测试替身，不访问微信。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.agents.provider import ChatHistory
from app.channels.ilink import QrStatus
from app.core.config import Settings
from app.main import create_app
from app.models.channel import ChannelAccount, ChannelBindSession, ChannelMessage, ChannelPeer
from tests.auth_helpers import add_identity, cleanup_identities
from tests.test_wechat_channel import FakeILink

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


class QuietProvider:
    """满足应用构造，本测试不调用模型。"""

    def validate(self) -> None:
        """替身可用。"""

    async def stream(self, history: ChatHistory):  # type: ignore[no-untyped-def]
        """不会被调用。"""
        del history
        yield "不会用到"

    async def summarize(self, previous: str, history: ChatHistory, max_characters: int) -> str:
        """不会被调用。"""
        del previous, history, max_characters
        return "不会用到"


@pytest.fixture
async def wechat_app():  # type: ignore[no-untyped-def]
    """在测试库启用渠道，并把 iLink 换成不会出网的替身。"""
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and str(target.database).endswith("_test")
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "EA_DATABASE_NAME": target.database},
        check=True,
    )
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "environment": "test",
            "task_scheduler_enabled": False,
            "schedule_enabled": False,
            "channel_token_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    app = create_app(settings, QuietProvider())
    fake = FakeILink()
    fake.qr_status = QrStatus(
        status="confirmed",
        bot_token="integration-token",
        ilink_bot_id="bot-integration",
        baseurl="https://ilinkai.weixin.qq.com",
        ilink_user_id="wx-integration",
    )
    async with app.router.lifespan_context(app):
        app.state.channel_service.ilink = fake
        app.state.channel_runtime.ilink = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            scope = await add_identity(app, client)
            yield app, client, scope
    await _delete_channels(app)
    await cleanup_identities(app)


async def _delete_channels(app: FastAPI) -> None:
    """删掉本夹具用户的渠道行，再交给身份清理。"""
    users = getattr(app.state, "test_users", [])
    async with app.state.database.session_factory.begin() as session:
        account_ids = select(ChannelAccount.id).where(ChannelAccount.user_id.in_(users))
        await session.execute(delete(ChannelMessage).where(ChannelMessage.account_id.in_(account_ids)))
        await session.execute(delete(ChannelPeer).where(ChannelPeer.account_id.in_(account_ids)))
        await session.execute(delete(ChannelBindSession).where(ChannelBindSession.user_id.in_(users)))
        await session.execute(delete(ChannelAccount).where(ChannelAccount.user_id.in_(users)))


async def test_bind_dedupe_and_pause_round_trip(wechat_app: tuple[FastAPI, AsyncClient, object]) -> None:
    """扫码确认写入密文，重复 message_id 不插入第二行，-14 会记下暂停时间。"""
    app, client, scope = wechat_app
    started = await client.post("/api/channels/wechat/bind")
    assert started.status_code == 201, started.text
    session_id = started.json()["session_id"]
    confirmed = await client.get(f"/api/channels/wechat/bind/{session_id}")
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["ilink_user_id"] == "wx-integration"
    assert "integration-token" not in confirmed.text
    listed = await client.get("/api/channels/wechat")
    assert listed.status_code == 200
    assert listed.json()[0]["status"] == "active"

    repo = app.state.channel_service.repo
    account = await repo.account_for_user(scope.user_id)  # type: ignore[attr-defined]
    assert account is not None
    assert account.token == "integration-token"
    first = await repo.record_inbound(account.id, "9007199254740993", "wx-integration", "received", None, "你好")
    second = await repo.record_inbound(account.id, "9007199254740993", "wx-integration", "received", None, "你好")
    assert first is True
    assert second is False
    from datetime import UTC, datetime, timedelta

    await repo.touch_inbound(account.id, account.workspace_id, "wx-integration", "ctx-1", datetime.now(UTC))
    peer = await repo.latest_peer(account.id)
    assert peer is not None and peer.context_token == "ctx-1"
    await repo.mark_paused(account.id, datetime.now(UTC) + timedelta(hours=1))
    paused = await repo.get_account(account.id)
    assert paused is not None and paused.status == "paused" and paused.paused_until is not None

    removed = await client.post(f"/api/channels/wechat/{account.id}/unbind")
    assert removed.status_code == 204
    gone = await repo.account_for_user(scope.user_id)  # type: ignore[attr-defined]
    assert gone is None
