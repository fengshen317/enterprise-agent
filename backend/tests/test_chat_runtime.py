"""不访问数据库的执行生命周期与取消竞态测试。"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agents.context_types import ContextMessage, ContextState
from app.agents.provider import ChatHistory, StreamFragment
from app.core.config import Settings
from app.core.database import Database
from app.core.errors import ApplicationError
from app.services.chat_runtime import ChatRuntime, injected_prefix
from app.services.chat_store import ChatStore


class TextProvider:
    """生成受控文本。"""

    def validate(self) -> None:
        """测试替身始终已配置。"""

    async def stream(self, history: ChatHistory, *, model: str | None = None) -> AsyncIterator[str]:
        """输出超过配置上限的片段。

        Args:
            history: 执行输入。

        Yields:
            边界测试正文。
        """
        del model
        yield "字" * 1001


@pytest.fixture
async def runtime() -> AsyncIterator[ChatRuntime]:
    """替换持久化边界，保留真实任务调度与退出逻辑。

    Yields:
        独立测试执行器。
    """
    settings = Settings(_env_file=None, environment="test", model_output_characters=1000)
    database = Database(settings)
    runtime = ChatRuntime(database, settings, TextProvider())
    runtime.store = AsyncMock(spec=ChatStore)
    runtime.store.begin_run.return_value = ContextState(uuid4(), "", 0, 0, [ContextMessage(1, "user", "问题")])
    runtime.store.snapshot.return_value = SimpleNamespace(run=SimpleNamespace(status="cancelled"))
    try:
        yield runtime
    finally:
        await runtime.close()
        await database.close()


async def test_output_limit_preserves_partial_answer(runtime: ChatRuntime) -> None:
    """超长输出保存上限以内的正文并标记失败。

    Args:
        runtime: 测试执行器。
    """
    run_id = uuid4()
    await runtime._execute(run_id, "request")
    runtime.store.finish.assert_awaited_once_with(
        run_id, "字" * 1000, "failed", "OUTPUT_TOO_LONG", "回答超过长度限制，请缩小问题范围"
    )


async def test_unclaimed_run_does_not_call_model(runtime: ChatRuntime) -> None:
    """终态或已被领取的运行不会重复生成。

    Args:
        runtime: 测试执行器。
    """
    runtime.store.begin_run.return_value = None
    await runtime._execute(uuid4(), "request")
    runtime.store.finish.assert_not_awaited()


async def test_cancel_before_coroutine_starts(runtime: ChatRuntime) -> None:
    """任务尚未获得执行机会时也必须提交取消终态。

    Args:
        runtime: 测试执行器。
    """
    run_id = uuid4()
    runtime.store.snapshot.return_value = SimpleNamespace(
        run=SimpleNamespace(status="pending"), message=SimpleNamespace(content="已保存")
    )
    runtime.schedule(run_id, "request")
    first = runtime.tasks[run_id]
    runtime.schedule(run_id, "duplicate")
    assert runtime.tasks[run_id] is first
    await runtime.cancel(run_id)
    runtime.store.finish.assert_awaited_once_with(run_id, "已保存", "cancelled")
    assert not runtime.tasks


async def test_cancel_without_owned_task(runtime: ChatRuntime) -> None:
    """提交成功后尚未分发的运行仍可取消。

    Args:
        runtime: 测试执行器。
    """
    run_id = uuid4()
    runtime.store.snapshot.return_value = SimpleNamespace(
        run=SimpleNamespace(status="running"), message=SimpleNamespace(content="部分文本")
    )
    await runtime.cancel(run_id)
    runtime.store.finish.assert_awaited_once_with(run_id, "部分文本", "cancelled")


def test_injected_prefix_reserves_retrieval_and_background() -> None:
    """检索资料和上一轮协作资料都计入即将注入的前缀。"""
    assert injected_prefix("", "") == ""
    prefix = injected_prefix("资料甲", "计划乙")
    assert "资料甲" in prefix and "计划乙" in prefix and prefix.endswith("本次用户要求：\n")


async def test_reasoning_is_saved_apart_from_content() -> None:
    """思考片段不写入正文，结束时单独交给存储。"""
    settings = Settings(_env_file=None, environment="test")
    database = Database(settings)
    runtime = ChatRuntime(database, settings, ReasoningProvider())
    runtime.store = AsyncMock(spec=ChatStore)
    runtime.store.begin_run.return_value = ContextState(uuid4(), "", 0, 0, [ContextMessage(1, "user", "问题")])
    try:
        await runtime._execute(uuid4(), "request")
    finally:
        await runtime.close()
        await database.close()
    runtime.store.finish.assert_awaited_once()
    assert runtime.store.finish.await_args.args[1] == "正文"
    assert runtime.store.finish.await_args.args[5] == "先分析"


class ReasoningProvider:
    """同时产出思考过程和正文。"""

    def validate(self) -> None:
        """测试替身无需配置。"""

    async def stream(self, history: ChatHistory, *, model: str | None = None) -> AsyncIterator[str | StreamFragment]:
        """先给出思考，再给出正文。

        Args:
            history: 上下文。
            model: 本次模型。

        Yields:
            思考片段和正文。
        """
        del history, model
        yield StreamFragment(reasoning="先分析")
        yield "正文"

    async def summarize(
        self, previous: str, history: ChatHistory, max_characters: int, *, model: str | None = None
    ) -> str:
        """测试不触发摘要。"""
        del previous, history, max_characters, model
        return "摘要"


async def test_shutdown_rejects_new_requests(runtime: ChatRuntime) -> None:
    """退出后不再创建或恢复新任务。

    Args:
        runtime: 测试执行器。
    """
    await runtime.close()
    with pytest.raises(ApplicationError, match="服务正在退出"):
        await runtime.ensure_ready()
