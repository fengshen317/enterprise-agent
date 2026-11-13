"""真实 PostgreSQL 问答契约验证，模型使用可控替身而非收费 API。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agents.provider import ChatHistory
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.main import create_app
from app.models.conversation import Conversation
from app.schemas.chat import RunCreate
from app.services.chat_runtime import ChatRuntime
from tests.auth_helpers import add_identity, cleanup_identities, copied_headers

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


class ControlledProvider:
    """记录上下文并支持阻塞、失败与空返回。"""

    def __init__(self) -> None:
        """初始化测试模式与同步信号。"""
        self.mode = "success"
        self.histories: list[ChatHistory] = []
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.summary_received = asyncio.Event()
        self.summary_calls: list[tuple[str, ChatHistory, int]] = []
        self.models: list[str | None] = []

    def validate(self) -> None:
        """模拟未配置模型的创建前拒绝。"""
        if self.mode == "missing":
            raise ApplicationError("MODEL_NOT_CONFIGURED", "未配置模型", 503)

    async def stream(self, history: ChatHistory, *, model: str | None = None) -> AsyncIterator[str]:
        """返回可控片段，异常包含私密串以验证脱敏。

        Args:
            history: 待校验上下文。

        Yields:
            测试回复片段。
        """
        self.histories.append(history)
        self.models.append(model)
        if self.mode == "reason":
            from app.agents.provider import StreamFragment

            yield StreamFragment(reasoning="内部推理不应进入摘要")
        if self.mode == "empty":
            return
        yield "已保存的部分回答。" * 32
        self.received.set()
        if self.mode == "wait":
            await self.release.wait()
        if self.mode == "fail":
            raise RuntimeError("private-api-key-and-provider-body")
        yield "完成。"

    async def summarize(
        self, previous: str, history: ChatHistory, max_characters: int, *, model: str | None = None
    ) -> str:
        """记录摘要输入并模拟失败或等待，日常测试不调用模型。

        Args:
            previous: 旧摘要。
            history: 新增历史。
            max_characters: 摘要预算。

        Returns:
            含固定事实的短摘要。
        """
        del model
        self.summary_calls.append((previous, history, max_characters))
        self.summary_received.set()
        if self.mode == "summary_fail":
            raise RuntimeError("private-summary-provider-response")
        if self.mode == "summary_wait":
            await self.release.wait()
        return "保留的项目代号为 COMPRESS-123。"


@pytest.fixture
async def chat() -> AsyncIterator[tuple[FastAPI, AsyncClient, ControlledProvider]]:
    """仅在指定测试库中运行，退出时清理本夹具创建的数据。

    Yields:
        应用、客户端与模型替身。
    """
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and target.database.endswith("_test")
    environment = {**os.environ, "EA_DATABASE_NAME": target.database}
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=environment,
        check=True,
    )
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "environment": "test",
            "task_scheduler_enabled": False,
            "schedule_enabled": False,
            "knowledge_enabled": False,
        }
    )
    provider = ControlledProvider()
    app = create_app(settings, provider)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            scope = await add_identity(app, client)
            app.state.test_scope = scope
            yield app, client, provider
        runtime: ChatRuntime = app.state.chat_runtime
        await runtime.close()
        await cleanup_identities(app)


async def new_conversation(client: AsyncClient) -> str:
    """创建带可清理前缀的测试对话。

    Args:
        client: 测试客户端。

    Returns:
        对话标识。
    """
    response = await client.post("/api/conversations", json={"title": "QA-TEST-" + uuid4().hex})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def submit(client: AsyncClient, conversation: str, content: str = "第一个问题", key: str = "key") -> dict:
    """提交测试问题。

    Args:
        client: 客户端。
        conversation: 对话标识。
        content: 问题内容。
        key: 幂等键。

    Returns:
        运行快照。
    """
    response = await client.post(
        f"/api/conversations/{conversation}/runs", json={"content": content, "idempotency_key": key}
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


async def settled(client: AsyncClient, run_id: str) -> dict:
    """等待后台运行结束，避免依赖固定睡眠时长。

    Args:
        client: 客户端。
        run_id: 运行标识。

    Returns:
        终态快照。
    """
    async with asyncio.timeout(10):
        while True:
            body = (await client.get(f"/api/runs/{run_id}")).json()
            if body["run"]["status"] not in ("pending", "running"):
                return dict(body)
            await asyncio.sleep(0.01)


async def test_conversation_roundtrip_and_sse(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """验证上下文、分页、幂等、历史与可重放终态 SSE。

    Args:
        chat: 完整测试应用。
    """
    _, client, provider = chat
    conversation = await new_conversation(client)
    first = await submit(client, conversation)
    run_id = first["run"]["id"]
    completed = await settled(client, run_id)
    assert completed["run"]["status"] == "completed"
    assert completed["message"]["content"].endswith("完成。")
    same = await submit(client, conversation)
    assert same["run"]["id"] == run_id
    assert len(provider.histories) == 1
    conflict = await client.post(
        f"/api/conversations/{conversation}/runs", json={"content": "不同", "idempotency_key": "key"}
    )
    assert conflict.status_code == 409
    second = await submit(client, conversation, "请继续", "followup")
    await settled(client, second["run"]["id"])
    assert provider.histories[1] == [
        ("user", "第一个问题"),
        ("assistant", completed["message"]["content"]),
        ("user", "请继续"),
    ]
    page = (await client.get(f"/api/conversations/{conversation}/messages?limit=2")).json()
    assert page["has_more"] and [item["sequence"] for item in page["items"]] == [1, 2]
    rest = (await client.get(f"/api/conversations/{conversation}/messages?after_sequence=2")).json()
    assert not rest["has_more"] and len(rest["items"]) == 2
    assert rest["active_run_id"] is None
    assert any(row["id"] == conversation for row in (await client.get("/api/conversations")).json())
    for _ in range(2):
        stream = await client.get(f"/api/runs/{run_id}/events")
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: completed" in stream.text and "完成。" in stream.text
    assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["run"]["status"] == "completed"


async def test_parallel_submit_and_cancel(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """验证并发提交只产生一组消息，取消保留部分内容并允许新运行。

    Args:
        chat: 完整测试应用。
    """
    _, client, provider = chat
    provider.mode = "wait"
    conversation = await new_conversation(client)
    path = f"/api/conversations/{conversation}/runs"
    bodies = [{"content": "并发问题", "idempotency_key": key} for key in ("one", "two")]
    responses = await asyncio.gather(*(client.post(path, json=body) for body in bodies))
    assert sorted(row.status_code for row in responses) == [202, 409]
    created = next(row.json() for row in responses if row.status_code == 202)
    run_id = created["run"]["id"]
    await asyncio.wait_for(provider.received.wait(), 5)
    page = (await client.get(f"/api/conversations/{conversation}/messages")).json()
    assert page["active_run_id"] == run_id
    assert len(page["items"]) == 2
    stopped = await client.post(f"/api/runs/{run_id}/cancel")
    assert stopped.json()["run"]["status"] == "cancelled"
    assert stopped.json()["message"]["status"] == "interrupted"
    assert "部分回答" in stopped.json()["message"]["content"]
    assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["run"]["status"] == "cancelled"
    provider.mode = "success"
    retry = await submit(client, conversation, "重试问题", "retry")
    await settled(client, retry["run"]["id"])
    assert provider.histories[-1] == [("user", "重试问题")]


@pytest.mark.parametrize(
    ("mode", "expected"), [("fail", "MODEL_CALL_FAILED"), ("empty", "EMPTY_MODEL_RESPONSE"), ("wait", "MODEL_TIMEOUT")]
)
async def test_failed_generation(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider], mode: str, expected: str
) -> None:
    """失败、超时和空回复均保存终态且不泄露模型异常。

    Args:
        chat: 完整测试应用。
        mode: 替身模式。
        expected: 安全错误码。
    """
    app, client, provider = chat
    provider.mode = mode
    if mode == "wait":
        app.state.chat_runtime.settings.chat_run_timeout_seconds = 0.1
    conversation = await new_conversation(client)
    run = await submit(client, conversation)
    result = await settled(client, run["run"]["id"])
    assert result["run"]["status"] == "failed"
    assert result["run"]["error_code"] == expected
    assert result["message"]["status"] == "interrupted"
    assert "private-api-key" not in str(result)


async def test_missing_and_invalid_requests(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """输入校验、资源不存在和未配置模型保持稳定契约。

    Args:
        chat: 完整测试应用。
    """
    _, client, provider = chat
    assert (await client.post("/api/conversations", json={"title": "  "})).status_code == 422
    assert (await client.get("/api/conversations?limit=201")).status_code == 422
    missing = uuid4()
    assert (await client.get(f"/api/conversations/{missing}/messages")).status_code == 404
    assert (await client.get(f"/api/runs/{missing}")).status_code == 404
    assert (await client.get(f"/api/runs/{missing}/events")).status_code == 404
    assert (await client.post(f"/api/runs/{missing}/cancel")).status_code == 404
    conversation = await new_conversation(client)
    assert (
        await client.post(f"/api/conversations/{conversation}/runs", json={"content": "  ", "idempotency_key": "a"})
    ).status_code == 422
    assert (
        await client.post(f"/api/conversations/{missing}/runs", json={"content": "hello", "idempotency_key": "a"})
    ).status_code == 404
    provider.mode = "missing"
    assert (
        await client.post(f"/api/conversations/{conversation}/runs", json={"content": "hello", "idempotency_key": "a"})
    ).status_code == 503
    assert (await client.get(f"/api/conversations/{conversation}/messages")).json()["items"] == []


async def test_recovery_and_single_process(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """重复后端拒绝抢占；重启后部分内容和中断原因保留。

    Args:
        chat: 完整测试应用。
    """
    app, client, provider = chat
    conversation = await new_conversation(client)
    competing = create_app(app.state.settings, provider)
    async with competing.router.lifespan_context(competing):
        async with AsyncClient(
            transport=ASGITransport(app=competing), base_url="http://other", headers=copied_headers(client)
        ) as other:
            response = await other.get("/api/conversations")
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "SINGLE_PROCESS_REQUIRED"
    runtime: ChatRuntime = app.state.chat_runtime
    snapshot = await runtime.store.create_run(
        UUID(conversation),
        RunCreate(content="遗留问题", idempotency_key="orphan"),
        "test",
        app.state.test_scope,
    )
    await runtime.store.save_content(snapshot.run.id, "重启前的内容")
    await runtime.close()
    replacement = create_app(app.state.settings, provider)
    async with replacement.router.lifespan_context(replacement):
        async with AsyncClient(
            transport=ASGITransport(app=replacement), base_url="http://new", headers=copied_headers(client)
        ) as fresh:
            recovered = (await fresh.get(f"/api/runs/{snapshot.run.id}")).json()
            assert recovered["run"]["status"] == "failed"
            assert recovered["run"]["error_code"] == "PROCESS_RESTARTED"
            assert recovered["message"]["content"] == "重启前的内容"


async def test_live_sse_disconnect_keeps_generation(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """通过真实 TCP 验证流式快照和浏览器断开不终止后台生成。

    Args:
        chat: 已运行生命周期的应用与模型替身。
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
        """使用启动事件同步客户端，避免按固定延迟猜测服务可用。"""

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            """启动真实监听并通知测试客户端。

            Args:
                sockets: 测试绑定的本机监听套接字。
            """
            await super().startup(sockets)
            ready.set()

    server = NotifyingServer(uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5, headers=copied_headers(client)
        ) as network:
            async with network.stream("GET", f"/api/runs/{run_id}/events") as stream:
                assert stream.status_code == 200
                async for line in stream.aiter_lines():
                    if line.startswith("data:"):
                        assert "部分回答" in line
                        break
            assert (await network.get(f"/api/runs/{run_id}")).json()["run"]["status"] == "running"
            stopped = await network.post(f"/api/runs/{run_id}/cancel")
            assert stopped.json()["run"]["status"] == "cancelled"
            assert "event: cancelled" in (await network.get(f"/api/runs/{run_id}/events")).text
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def summary_state(app: FastAPI, conversation_id: str) -> tuple[str, int, int]:
    """读取数据库权威摘要，避免将模拟模型输出视作已持久化。

    Args:
        app: 测试应用。
        conversation_id: 对话标识。

    Returns:
        摘要、覆盖序号及版本。
    """
    async with app.state.database.session_factory() as session:
        row = await session.get(Conversation, UUID(conversation_id))
        return row.summary, row.summary_through_sequence, row.summary_version


async def test_persisted_compression_reused_after_restart(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """压缩可跨运行和应用实例复用，后续摘要不重复读取旧前缀。"""
    app, client, provider = chat
    app.state.settings.model_context_characters = 1000
    conversation = await new_conversation(client)
    original = "项目代号为 COMPRESS-123。" + "历史背景" * 100
    first = await submit(client, conversation, original)
    await settled(client, first["run"]["id"])
    second = await submit(client, conversation, "继续", "second")
    assert (await settled(client, second["run"]["id"]))["run"]["status"] == "completed"
    persisted = await summary_state(app, conversation)
    assert persisted == ("保留的项目代号为 COMPRESS-123。", 2, 1)
    assert len(provider.summary_calls) == 1
    assert provider.summary_calls[0][1][0] == ("user", original)
    assert all(content != "继续" for _, content in provider.summary_calls[0][1])
    assert provider.histories[1][0][0] == "user"
    assert "COMPRESS-123" in provider.histories[1][0][1]
    third = await submit(client, conversation, "代号？", "third")
    await settled(client, third["run"]["id"])
    assert len(provider.summary_calls) == 1
    assert await summary_state(app, conversation) == persisted
    history = (await client.get(f"/api/conversations/{conversation}/messages")).json()["items"]
    assert len(history) == 6 and history[0]["content"] == original
    await app.state.chat_runtime.close()
    replacement = create_app(app.state.settings, provider)
    async with replacement.router.lifespan_context(replacement):
        async with AsyncClient(
            transport=ASGITransport(app=replacement), base_url="http://replacement", headers=copied_headers(client)
        ) as fresh:
            fourth = await submit(fresh, conversation, "确认？", "fourth")
            assert (await settled(fresh, fourth["run"]["id"]))["run"]["status"] == "completed"
            assert len(provider.summary_calls) == 1
            fifth = await submit(fresh, conversation, "再确认", "fifth")
            await settled(fresh, fifth["run"]["id"])
            updated = await summary_state(replacement, conversation)
            assert updated[1] > 2 and updated[2] == 2
            assert provider.summary_calls[1][0] == persisted[0]
            assert all(original not in content for _, content in provider.summary_calls[1][1])
            assert len((await fresh.get(f"/api/conversations/{conversation}/messages")).json()["items"]) == 10


async def test_compression_failure_and_cancel_leave_cursor_unchanged(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """失败与摘要中途取消不提交新摘要、不丢失历史。"""
    app, client, provider = chat
    app.state.settings.model_context_characters = 1000
    conversation = await new_conversation(client)
    first = await submit(client, conversation, "背景" * 220)
    await settled(client, first["run"]["id"])
    provider.mode = "summary_fail"
    second = await submit(client, conversation, "继续", "failure")
    result = await settled(client, second["run"]["id"])
    assert result["run"]["error_code"] == "CONTEXT_COMPRESSION_FAILED"
    assert "private" not in str(result)
    assert await summary_state(app, conversation) == ("", 0, 0)
    provider.mode = "summary_wait"
    provider.summary_received.clear()
    third = await submit(client, conversation, "再试", "cancel")
    await asyncio.wait_for(provider.summary_received.wait(), 5)
    stopped = (await client.post(f"/api/runs/{third['run']['id']}/cancel")).json()
    assert stopped["run"]["status"] == "cancelled"
    assert await summary_state(app, conversation) == ("", 0, 0)
    assert len(provider.histories) == 1
    provider.mode = "success"
    fourth = await submit(client, conversation, "重试", "retry-summary")
    assert (await settled(client, fourth["run"]["id"]))["run"]["status"] == "completed"
    assert (await summary_state(app, conversation))[1:] == (2, 1)


async def test_summary_version_and_boundary_guard(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """数据库提交拒绝旧版本、未完成消息边界和已取消运行。"""
    app, client, _ = chat
    conversation = await new_conversation(client)
    first = await submit(client, conversation)
    await settled(client, first["run"]["id"])
    store = app.state.chat_runtime.store
    pending = await store.create_run(
        UUID(conversation), RunCreate(content="新问题", idempotency_key="manual"), "test", app.state.test_scope
    )
    await store.begin_run(pending.run.id)
    with pytest.raises(ApplicationError) as conflict:
        await store.save_summary(pending.run.id, 1, 2, "摘要", "test")
    assert conflict.value.code == "SUMMARY_STATE_CHANGED"
    for boundary in (1, 3, 100):
        with pytest.raises(ApplicationError) as invalid:
            await store.save_summary(pending.run.id, 0, boundary, "摘要", "test")
        assert invalid.value.code == "INVALID_SUMMARY_BOUNDARY"
    assert await store.save_summary(pending.run.id, 0, 2, "摘要", "test")
    with pytest.raises(ApplicationError):
        await store.save_summary(pending.run.id, 0, 2, "旧版本", "test")
    await store.cancel(pending.run.id)
    assert not await store.save_summary(pending.run.id, 1, 2, "取消后的摘要", "test")
    await app.state.chat_runtime.cancel(pending.run.id)
    assert await summary_state(app, conversation) == ("摘要", 2, 1)


async def test_model_selection_regenerate_and_usage(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """对话级模型生效，重新生成不复制用户消息，用量标记为估算。"""
    _, client, provider = chat
    options = (await client.get("/api/models")).json()
    assert {item["id"] for item in options} >= {"deepseek-flash", "deepseek-chat", "deepseek-reasoner"}
    conversation = await new_conversation(client)
    denied = await client.post(f"/api/conversations/{conversation}/model", json={"model": "gpt-4"})
    assert denied.status_code == 422 and denied.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    selected = await client.post(f"/api/conversations/{conversation}/model", json={"model": "deepseek-chat"})
    assert selected.status_code == 200 and selected.json()["model"] == "deepseek-chat"
    created = await submit(client, conversation, "第一个问题", "model-key")
    assert created["run"]["model"] == "deepseek-chat"
    await settled(client, created["run"]["id"])
    assert provider.models[-1] == "deepseek-chat"
    page = (await client.get(f"/api/conversations/{conversation}/messages")).json()
    assert page["context_usage"]["estimate"] is True
    assert page["context_usage"]["limit_characters"] > 0
    assert len(page["items"]) == 2
    assistant = page["items"][1]
    user = page["items"][0]
    regenerated = await client.post(f"/api/messages/{assistant['id']}/regenerate", json={"idempotency_key": "regen-1"})
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["run"]["id"] != created["run"]["id"]
    await settled(client, regenerated.json()["run"]["id"])
    again = (await client.get(f"/api/conversations/{conversation}/messages")).json()
    assert [item["role"] for item in again["items"]] == ["user", "assistant"]
    assert again["items"][0]["content"] == "第一个问题"
    assert again["items"][1]["run_id"] == regenerated.json()["run"]["id"]
    assert provider.histories[-1] == [("user", "第一个问题")]
    edited = await client.post(
        f"/api/messages/{user['id']}/edit",
        json={"content": "改写后的问题", "idempotency_key": "edit-1"},
    )
    assert edited.status_code == 202, edited.text
    await settled(client, edited.json()["run"]["id"])
    final = (await client.get(f"/api/conversations/{conversation}/messages")).json()
    assert len(final["items"]) == 2
    assert final["items"][0]["content"] == "改写后的问题"
    assert provider.histories[-1] == [("user", "改写后的问题")]
    follow = await submit(client, conversation, "下一问", "follow")
    await settled(client, follow["run"]["id"])
    older = await client.post(f"/api/messages/{assistant['id']}/regenerate", json={"idempotency_key": "regen-old"})
    assert older.status_code == 409 and older.json()["error"]["code"] == "NOT_LAST_TURN"


async def test_reasoning_stays_out_of_later_context(chat: tuple[FastAPI, AsyncClient, ControlledProvider]) -> None:
    """思考过程单独保存，后续上下文和摘要只看到正文。"""
    app, client, provider = chat
    provider.mode = "reason"
    conversation = await new_conversation(client)
    first = await submit(client, conversation, "需要推理的问题", "reason-1")
    completed = await settled(client, first["run"]["id"])
    assert completed["message"]["reasoning"] == "内部推理不应进入摘要"
    assert "内部推理" not in completed["message"]["content"]
    provider.mode = "success"
    second = await submit(client, conversation, "继续", "reason-2")
    await settled(client, second["run"]["id"])
    assert "内部推理" not in json.dumps(provider.histories[-1], ensure_ascii=False)
    assert await summary_state(app, conversation) == ("", 0, 0)
