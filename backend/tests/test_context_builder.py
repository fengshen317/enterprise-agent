"""上下文压缩策略测试，不调用真实模型或数据库。"""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.agents.context_types import ContextMessage, ContextState
from app.agents.provider import StreamProvider
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.services.chat_store import ChatStore
from app.services.context_builder import SUMMARY_PREFIX, ContextBuilder, summary_batches


def make_state(turns: int = 3, current: str = "当前问题", summary: str = "", through: int = 0) -> ContextState:
    """生成包含明显角色与轮次的上下文。

    Args:
        turns: 成功历史轮数。
        current: 当前问题。
        summary: 旧摘要。
        through: 旧摘要边界。

    Returns:
        可用于策略验证的运行快照。
    """
    messages = []
    for index in range(turns):
        messages.extend(
            [
                ContextMessage(through + index * 2 + 1, "user", f"问题{index}" + "问" * 150),
                ContextMessage(through + index * 2 + 2, "assistant", f"回答{index}" + "答" * 150),
            ]
        )
    messages.append(ContextMessage(through + turns * 2 + 1, "user", current))
    return ContextState(uuid4(), summary, through, 1 if summary else 0, messages)


def builder(**changes: object) -> tuple[ContextBuilder, AsyncMock, AsyncMock]:
    """提供可观察的摘要与持久化边界。

    Args:
        changes: 覆盖的测试配置。

    Returns:
        构建器及两个依赖替身。
    """
    config = Settings(_env_file=None, model_context_characters=1000, **changes)
    store, provider = AsyncMock(spec=ChatStore), AsyncMock(spec=StreamProvider)
    store.save_summary.return_value = True
    provider.summarize.return_value = "保留项目代号、约定与尚未解决事项。"
    return ContextBuilder(store, provider, config), store, provider


async def test_short_context_and_persisted_summary_reused() -> None:
    """短对话不触发摘要，已有摘要作为背景资料复用。"""
    context, store, provider = builder()
    state = make_state(1, summary="旧项目约定", through=8)
    history = await context.build(state)
    assert history[0] == ("user", SUMMARY_PREFIX + "旧项目约定")
    assert history[-1] == ("user", "当前问题")
    provider.summarize.assert_not_awaited()
    store.save_summary.assert_not_awaited()


async def test_incremental_summary_keeps_complete_recent_turn() -> None:
    """只压缩旧轮次，保留最近一轮与当前输入，不改变原快照。"""
    context, store, provider = builder()
    state = make_state(3, summary="上一摘要", through=10)
    before = list(state.messages)
    history = await context.build(state)
    previous, source, maximum = provider.summarize.call_args.args
    assert previous == "上一摘要" and maximum == 200
    assert source == [(item.role, item.content) for item in state.messages[:4]]
    assert history[1:] == [(item.role, item.content) for item in state.messages[4:]]
    assert sum(len(content) for _, content in history) < 700
    assert state.messages == before
    store.save_summary.assert_awaited_once_with(
        state.run_id, 1, 14, "保留项目代号、约定与尚未解决事项。", "deepseek-flash"
    )


async def test_large_current_question_is_never_summarized() -> None:
    """单个问题无法容纳时明确失败，不把它纳入摘要或截断。"""
    context, store, provider = builder()
    with pytest.raises(ApplicationError) as result:
        await context.build(make_state(0, current="当前" * 600))
    assert result.value.code == "CONTEXT_TOO_LONG"
    provider.summarize.assert_not_awaited()
    store.save_summary.assert_not_awaited()


async def test_disabled_compression_preserves_hard_limit() -> None:
    """关闭自动压缩时仍复用已有摘要，但保留长度保护。"""
    context, store, provider = builder(context_compression_enabled=False)
    with pytest.raises(ApplicationError) as result:
        await context.build(make_state(4))
    assert result.value.code == "CONTEXT_TOO_LONG"
    provider.summarize.assert_not_awaited()
    store.save_summary.assert_not_awaited()


@pytest.mark.parametrize("answer", ["", "  ", "字" * 201, RuntimeError("private-key-response")])
async def test_bad_summary_does_not_advance_cursor(answer: str | Exception) -> None:
    """摘要空、超长或模型异常时不提交摘要和游标。"""
    context, store, provider = builder()
    if isinstance(answer, Exception):
        provider.summarize.side_effect = answer
    else:
        provider.summarize.return_value = answer
    with pytest.raises(ApplicationError) as result:
        await context.build(make_state(3))
    assert result.value.code == "CONTEXT_COMPRESSION_FAILED"
    assert "private-key" not in result.value.message
    store.save_summary.assert_not_awaited()


async def test_cancel_and_timeout_do_not_save_summary() -> None:
    """模型等待被取消或超时时旧摘要仍完整保留。"""
    context, store, provider = builder(task_retry_base_seconds=0.01, task_retry_max_seconds=0.02)
    provider.summarize.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await context.build(make_state(3))
    provider.summarize.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await context.build(make_state(3))
    store.save_summary.assert_not_awaited()
    assert provider.summarize.await_count > 2


async def test_cancellation_before_commit_prevents_answer() -> None:
    """取消已持久化后，摘要不能提交或继续生成回答。"""
    context, store, _ = builder()
    store.save_summary.return_value = False
    with pytest.raises(asyncio.CancelledError):
        await context.build(make_state(3))


async def test_batches_are_merged_in_order_and_saved_once() -> None:
    """多批摘要合并上一个结果，仅最后一次更新数据库游标。"""
    context, store, provider = builder(context_summary_batch_characters=1000)
    state = make_state(15)
    await context.build(state)
    assert provider.summarize.await_count > 1
    assert provider.summarize.call_args_list[1].args[0] == provider.summarize.return_value
    store.save_summary.assert_awaited_once()


async def test_injected_material_is_counted_before_compression() -> None:
    """检索和协作资料占用预算，必要时把更早轮次纳入摘要。"""
    context, _, provider = builder()
    state = make_state(1)
    await context.build(state)
    provider.summarize.assert_not_awaited()
    context, _, provider = builder()
    await context.build(make_state(1), extra_characters=500)
    provider.summarize.assert_awaited()
    assert context.last_usage is not None
    assert context.last_usage.injected_characters == 500
    assert context.last_usage.estimated_characters > 500


async def test_summary_retries_transient_network_error() -> None:
    """摘要遇到瞬时网络错误时退避重试，成功后才提交游标。"""
    context, store, provider = builder(task_retry_base_seconds=0.01, task_retry_max_seconds=0.02)
    provider.summarize.side_effect = [httpx.ConnectError("reset"), "保留项目代号、约定与尚未解决事项。"]
    await context.build(make_state(3))
    assert provider.summarize.await_count == 2
    store.save_summary.assert_awaited()


def test_split_large_escaped_messages_without_losing_content() -> None:
    """JSON 转义、Unicode 与长消息都计入预算，源内容可完整重组。"""
    first = '换行\n引号"控制\x00中文🙂' * 1000
    history = [("user", first), ("assistant", "回复" * 500)]
    batches = list(summary_batches(history, 1000))
    assert len(batches) > 2
    for batch in batches:
        assert len(json.dumps([{"role": role, "content": value} for role, value in batch], ensure_ascii=False)) <= 1000
    assert "".join(value for batch in batches for role, value in batch if role == "user") == first
    assert "".join(value for batch in batches for role, value in batch if role == "assistant") == "回复" * 500
    assert list(summary_batches([], 1000)) == []
    with pytest.raises(ApplicationError, match="摘要输入预算不足"):
        list(summary_batches([("user", "test")], 10))


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [ContextMessage(1, "assistant", "无输入")],
        [ContextMessage(1, "assistant", "错序"), ContextMessage(2, "user", "错序"), ContextMessage(3, "user", "当前")],
    ],
)
async def test_invalid_context_is_rejected(messages: list[ContextMessage]) -> None:
    """损坏的角色顺序不得进入压缩流程。"""
    context, _, provider = builder()
    with pytest.raises(ApplicationError) as result:
        await context.build(ContextState(uuid4(), "", 0, 0, messages))
    assert result.value.code == "CONTEXT_STATE_INVALID"
    provider.summarize.assert_not_awaited()
