"""RSS 插件从安装配置到工具装配的数据库与 HTTP 回归。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.connectors.rss import FeedEntry
from app.connectors.rss_sources import RSS_SOURCE_PRESETS
from app.models.connector import Connector
from app.models.identity import Workspace
from app.models.tool_call import ToolCall
from app.schemas.chat import RunCreate
from app.services.tools import ToolExecutor
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration


async def test_rss_install_configure_call_and_restart(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置先校验再保存，普通成员不能改订阅，重建注册表仍可读取配置。"""
    app, client, _ = chat
    registry = app.state.connector_registry
    factory = app.state.database.session_factory
    entry = FeedEntry(
        "guid",
        "AI 发布",
        "https://example.com/article",
        "摘要",
        "Official",
        "https://example.com/feed",
        datetime.now(UTC).isoformat(),
        "pubDate",
    )
    monkeypatch.setattr("app.connectors.rss.fetch_feed", AsyncMock(return_value=[entry]))
    try:
        presets = await client.get("/api/connectors/rss/sources")
        assert presets.status_code == 200 and len(presets.json()) == len(RSS_SOURCE_PRESETS)
        assert presets.json()[0]["url"] == RSS_SOURCE_PRESETS[0]["url"]
        ready = [item for item in presets.json() if item["url"]]
        assert len(ready) == 10
        for publisher in ("Anthropic", "xAI", "DeepSeek", "豆包", "Kimi"):
            assert any(publisher in item["name"] for item in ready)
        pending = [item for item in presets.json() if not item["url"]]
        assert len(pending) == 1 and "X / Twitter" in pending[0]["name"]
        assert "待配置" in pending[0]["description"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anonymous:
            assert (await anonymous.get("/api/connectors/rss/sources")).status_code == 401
        installed = await client.post("/api/admin/connectors/rss/install")
        assert installed.status_code == 200 and installed.json()["config_fields"][0]["field_type"] == "json"
        saved = await client.put(
            "/api/admin/connectors/rss/config",
            json={
                "config": {"feeds": ["https://example.com/feed"]},
            },
        )
        assert saved.status_code == 200
        invalid = await client.put("/api/admin/connectors/rss/config", json={"config": {"feeds": "invalid-json"}})
        assert invalid.status_code == 422
        current = (await client.get("/api/connectors/rss")).json()
        assert current["config_values"]["feeds"] == '["https://example.com/feed"]'
        enabled = await client.post("/api/admin/connectors/rss/enabled", json={"enabled": True})
        assert enabled.status_code == 200 and enabled.json()["auth_status"] == "not_required"
        await registry.refresh("rss")
        tool = app.state.tool_registry.resolve("rss__recent")
        assert tool is not None and tool.risk == "read"
        connector = registry.resolve("rss")
        assert connector is not None
        result = await connector.call("recent", {"hours": 24})
        assert result["returned_count"] == 1
        runtime = app.state.chat_runtime
        conversation = UUID(await new_conversation(client))
        snapshot = await runtime.create_run(
            conversation, RunCreate(content="RSS 简报", idempotency_key="rss-executor"), app.state.test_scope
        )
        await runtime.store.begin_run(snapshot.run.id)
        executor = ToolExecutor(factory, runtime.settings, snapshot.run.id, registry=app.state.tool_registry)
        invoked = json.loads(await executor.invoke("rss__recent", {"hours": 24}))
        assert invoked["ok"] and invoked["data"]["items"][0]["url"] == entry.url
        async with factory() as session:
            audit = await session.scalar(select(ToolCall).where(ToolCall.run_id == snapshot.run.id))
            assert audit is not None and audit.status == "completed" and audit.tool_name == "rss__recent"

        async def partially_available(url: str) -> list[FeedEntry]:
            if "broken" in url:
                from app.connectors.base import ConnectorError

                raise ConnectorError("timeout", "timeout")
            return [entry]

        monkeypatch.setattr("app.connectors.rss.fetch_feed", partially_available)
        partial = await client.put(
            "/api/admin/connectors/rss/config",
            json={
                "config": {"feeds": ["https://example.com/feed", "https://broken.example/feed"]},
            },
        )
        assert partial.status_code == 200
        # 使用刷新后的工具实例，验证正常工具调用不会把部分失败标为全源健康。
        executor = ToolExecutor(factory, runtime.settings, snapshot.run.id, registry=app.state.tool_registry)
        invoked = json.loads(await executor.invoke("rss__recent", {"hours": 24}))
        assert invoked["ok"] and invoked["data"]["status"] == "partial"
        assert (await client.get("/api/connectors/rss")).json()["status"] == "degraded"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as member:
            async with factory() as session:
                slug = await session.scalar(
                    select(Workspace.slug).where(Workspace.id == app.state.test_scope.workspace_id)
                )
            await add_identity(app, member, workspace=slug, role="member")
            response = await member.put("/api/admin/connectors/rss/config", json={"config": {"feeds": "[]"}})
            assert response.status_code == 403
        stopped = await client.post("/api/admin/connectors/rss/enabled", json={"enabled": False})
        assert stopped.status_code == 200 and registry.resolve("rss") is None
        assert app.state.tool_registry.resolve("rss__recent") is None
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "rss"))
        await registry.refresh("rss")
