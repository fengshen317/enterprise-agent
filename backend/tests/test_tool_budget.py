"""使用真实预算策略验证有效检索空间和无进展调用拦截。"""

import asyncio

import pytest

from app.connectors.base import ConnectorError
from app.connectors.personal import PersonalConnector
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.services.tool_budget import ToolProgressGuard, effective_tool_limit


def test_personal_search_and_read_have_separate_budgets() -> None:
    """邮件搜索和逐封阅读不再共用四次的默认保护值。"""
    settings = Settings(_env_file=None)
    connector = PersonalConnector("qq_mail", settings)
    specs = connector.tool_specs(list(connector.definitions), {})
    assert [effective_tool_limit(spec, settings) for spec in specs] == [8, 15]
    assert settings.tool_total_calls_per_run == 30
    stricter = settings.model_copy(update={"tool_max_calls_per_run": 2})
    assert [effective_tool_limit(spec, stricter) for spec in specs] == [2, 2]


async def test_repeat_guard_normalizes_arguments_and_preserves_other_queries() -> None:
    """相同查询连续三次返回相同结果后拦截，翻页和新关键词仍可执行。"""
    guard = ToolProgressGuard(3)
    calls = 0

    async def fetch() -> object:
        """模拟稳定的上游查询结果。"""
        nonlocal calls
        calls += 1
        return {"items": [{"id": "meeting-1", "subject": "private-subject"}]}

    for _ in range(3):
        await guard.execute("search_mail", {"page": 1, "query": "private-query"}, fetch)
    with pytest.raises(ApplicationError, match="没有带来新信息"):
        await guard.execute("search_mail", {"query": "private-query", "page": 1}, fetch)
    assert calls == 3
    await guard.execute("search_mail", {"query": "private-query", "page": 2}, fetch)
    await guard.execute("search_mail", {"query": "different-query", "page": 1}, fetch)
    assert calls == 5
    assert "private" not in repr(guard.__dict__)
    # 新一轮执行器拥有新的检测状态，可以继续查询当前数据。
    await ToolProgressGuard(3).execute("search_mail", {"page": 1, "query": "private-query"}, fetch)


async def test_changed_results_reset_no_progress_counter() -> None:
    """返回了新资料时允许继续，即使查询参数没有变化。"""
    guard = ToolProgressGuard(3)
    calls = 0

    async def fetch() -> object:
        """返回持续更新的业务结果。"""
        nonlocal calls
        calls += 1
        return {"revision": calls}

    for _ in range(6):
        await guard.execute("read_data", {}, fetch)
    assert calls == 6


async def test_parallel_identical_requests_cannot_bypass_repeat_guard() -> None:
    """只串行化相同查询路径，避免并发绕过无进展阈值。"""
    guard = ToolProgressGuard(3)
    calls = 0

    async def fetch() -> object:
        """让相同查询有交叠的调度机会。"""
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"items": []}

    outcomes = await asyncio.gather(*(guard.execute("search", {}, fetch) for _ in range(6)), return_exceptions=True)
    assert calls == 3
    assert sum(isinstance(value, ApplicationError) for value in outcomes) == 3


async def test_independent_queries_can_run_in_parallel() -> None:
    """不同邮件或分页不因重复检测而失去并行能力。"""
    guard = ToolProgressGuard(3)
    both_entered = asyncio.Event()
    entered = 0

    async def fetch() -> object:
        """等待两个不同查询都进入上游调用。"""
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 2)
        return {"ok": True}

    await asyncio.gather(guard.execute("read", {"id": "first"}, fetch), guard.execute("read", {"id": "second"}, fetch))


async def test_repeated_errors_are_bounded_without_retaining_private_output() -> None:
    """相同故障重复出现也要停止，异常正文不能进入检测状态。"""
    guard = ToolProgressGuard(3)

    async def fetch() -> object:
        """模拟含敏感文本的上游错误。"""
        raise ConnectorError("network_error", "private-credential")

    for _ in range(3):
        with pytest.raises(ConnectorError):
            await guard.execute("search", {}, fetch)
    with pytest.raises(ApplicationError) as error:
        await guard.execute("search", {}, fetch)
    assert error.value.code == "TOOL_NO_PROGRESS"
    assert "private" not in repr(guard.__dict__)
