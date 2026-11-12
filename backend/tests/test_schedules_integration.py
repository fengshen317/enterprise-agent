"""真实 PostgreSQL 下的定时问答契约验证，模型使用可控替身。"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.agents.provider import ChatHistory
from app.core.config import Settings
from app.main import create_app
from app.models.schedule import ScheduledTask
from tests.auth_helpers import add_identity, cleanup_identities

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


class ScheduledProvider:
    """提供固定正文，验证调度链路而不调用真实模型。"""

    def validate(self) -> None:
        """测试替身始终可用。"""

    async def stream(self, history: ChatHistory) -> AsyncIterator[str]:
        """返回可断言的固定回答。

        Args:
            history: 已构建的对话历史。

        Yields:
            固定正文。
        """
        yield "定时回答。"

    async def summarize(self, previous: str, history: ChatHistory, max_characters: int) -> str:
        """测试不触发摘要时返回占位摘要。

        Args:
            previous: 旧摘要。
            history: 新增历史。
            max_characters: 摘要预算。

        Returns:
            固定摘要。
        """
        return "定时摘要"


@pytest.fixture
async def schedules() -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """仅在专用测试库运行，退出时清理本次身份与定时任务。

    Yields:
        应用与已登录客户端。
    """
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and target.database.endswith("_test")
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
        }
    )
    app = create_app(settings, ScheduledProvider())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await add_identity(app, client)
            yield app, client
    await cleanup_identities(app)


async def new_conversation(client: AsyncClient, title: str = "SCHEDULE-TEST") -> str:
    """创建可清理的测试对话。

    Args:
        client: 已登录客户端。
        title: 对话标题。

    Returns:
        对话标识。
    """
    response = await client.post("/api/conversations", json={"title": title})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def schedule_body(conversation_id: str, **changes: object) -> dict[str, object]:
    """构造默认定时任务输入。

    Args:
        conversation_id: 固定对话标识。
        changes: 覆盖字段。

    Returns:
        请求体。
    """
    body: dict[str, object] = {
        "name": "每日晨报",
        "prompt": "请总结今天的重点事项。",
        "conversation_id": conversation_id,
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "enabled": True,
    }
    body.update(changes)
    return body


async def wait_terminal(client: AsyncClient, run_id: str, wait_seconds: float = 10) -> dict[str, object]:
    """等待后台生成进入终态。

    Args:
        client: 已登录客户端。
        run_id: 运行标识。
        wait_seconds: 最大等待秒数。

    Returns:
        终态快照。
    """
    deadline = monotonic() + wait_seconds
    while monotonic() < deadline:
        response = await client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        snapshot: dict[str, object] = response.json()
        run = snapshot["run"]
        assert isinstance(run, dict)
        if run["status"] not in ("pending", "running"):
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError("定时问答未在超时内完成")


async def test_schedule_crud_and_input_validation(schedules: tuple[FastAPI, AsyncClient]) -> None:
    """创建、列表、停用和删除均只作用于当前用户。"""
    app, client = schedules
    conversation_id = await new_conversation(client)
    created = await client.post("/api/schedules", json=schedule_body(conversation_id))
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["conversation_id"] == conversation_id
    assert item["timezone"] == "Asia/Shanghai"
    assert item["next_run_at"] is not None
    assert item["enabled"] is True

    listed = await client.get("/api/schedules")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [item["id"]]

    disabled = await client.post(f"/api/schedules/{item['id']}", json=schedule_body(conversation_id, enabled=False))
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["next_run_at"] is None

    invalid_cron = await client.post(
        f"/api/schedules/{item['id']}", json=schedule_body(conversation_id, cron_expression="0 9 * *")
    )
    assert invalid_cron.status_code == 422
    assert invalid_cron.json()["error"]["code"] == "INVALID_CRON"

    invalid_timezone = await client.post(
        f"/api/schedules/{item['id']}", json=schedule_body(conversation_id, timezone="Asia/Not-A-Zone")
    )
    assert invalid_timezone.status_code == 422
    assert invalid_timezone.json()["error"]["code"] == "INVALID_TIMEZONE"

    deleted = await client.post(f"/api/schedules/{item['id']}/delete")
    assert deleted.status_code == 204
    assert (await client.get("/api/schedules")).json() == []


async def test_run_now_appends_to_fixed_conversation(schedules: tuple[FastAPI, AsyncClient]) -> None:
    """手动触发也在固定对话中新增成对消息，不创建新对话。"""
    app, client = schedules
    conversation_id = await new_conversation(client)
    created = await client.post("/api/schedules", json=schedule_body(conversation_id))
    schedule_id = created.json()["id"]

    first = await client.post(f"/api/schedules/{schedule_id}/run")
    assert first.status_code == 202, first.text
    first_snapshot = await wait_terminal(client, first.json()["run"]["id"])
    assert first_snapshot["run"]["status"] == "completed"
    assert first_snapshot["run"]["conversation_id"] == conversation_id
    assert first_snapshot["message"]["content"] == "定时回答。"

    second = await client.post(f"/api/schedules/{schedule_id}/run")
    assert second.status_code == 202, second.text
    await wait_terminal(client, second.json()["run"]["id"])

    messages = await client.get(f"/api/conversations/{conversation_id}/messages")
    items = messages.json()["items"]
    assert len(items) == 4
    assert [row["role"] for row in items] == ["user", "assistant", "user", "assistant"]
    assert items[0]["content"] == "请总结今天的重点事项。"


async def test_due_claim_advances_schedule_and_skips_disabled(schedules: tuple[FastAPI, AsyncClient]) -> None:
    """到期领取先推进下一次时间，并记录真实运行；停用任务不再领取。"""
    app, client = schedules
    conversation_id = await new_conversation(client)
    created = await client.post("/api/schedules", json=schedule_body(conversation_id, cron_expression="*/5 * * * *"))
    schedule_id = UUID(created.json()["id"])
    past = datetime.now(UTC) - timedelta(minutes=1)
    async with app.state.database.session_factory.begin() as session:
        await session.execute(update(ScheduledTask).where(ScheduledTask.id == schedule_id).values(next_run_at=past))

    assert await app.state.schedule_runtime.run_due_once() is True
    rows = (await client.get("/api/schedules")).json()
    row = rows[0]
    assert row["last_status"] == "started"
    assert row["last_run_id"] is not None
    assert datetime.fromisoformat(row["next_run_at"]) > past
    snapshot = await wait_terminal(client, row["last_run_id"])
    assert snapshot["run"]["status"] == "completed"

    disabled = await client.post(f"/api/schedules/{schedule_id}", json=schedule_body(conversation_id, enabled=False))
    assert disabled.status_code == 200
    async with app.state.database.session_factory.begin() as session:
        await session.execute(update(ScheduledTask).where(ScheduledTask.id == schedule_id).values(next_run_at=past))
    assert await app.state.schedule_runtime.run_due_once() is False


async def test_schedule_hidden_from_other_user(schedules: tuple[FastAPI, AsyncClient]) -> None:
    """定时任务按用户隔离，其他用户不能读取、修改或触发。"""
    app, client = schedules
    conversation_id = await new_conversation(client)
    created = await client.post("/api/schedules", json=schedule_body(conversation_id))
    schedule_id = created.json()["id"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await add_identity(app, other)
        assert (await other.get("/api/schedules")).json() == []
        updated = await other.post(f"/api/schedules/{schedule_id}", json=schedule_body(conversation_id))
        assert updated.status_code == 404
        triggered = await other.post(f"/api/schedules/{schedule_id}/run")
        assert triggered.status_code == 404
