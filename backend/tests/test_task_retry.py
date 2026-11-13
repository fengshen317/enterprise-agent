"""结构化计划校验与集中网络重试策略的纯单元验证。"""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.task import PlanDraft


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"key": "one", "title": "一", "kind": "analysis"}, {"key": "one", "title": "重复", "kind": "analysis"}],
        [{"key": "one", "title": "未知依赖", "kind": "analysis", "depends_on": ["missing"]}],
        [{"key": "one", "title": "自环", "kind": "analysis", "depends_on": ["one"]}],
        [
            {"key": "one", "title": "一", "kind": "analysis", "depends_on": ["two"]},
            {"key": "two", "title": "二", "kind": "analysis", "depends_on": ["one"]},
        ],
        [{"key": "one", "title": "越权工具", "kind": "tool", "tool_name": "execute"}],
        [
            {
                "key": "one",
                "title": "伪造身份",
                "kind": "tool",
                "tool_name": "get_my_run_stats",
                "arguments": {"days": 7, "workspace_id": "spoof"},
            }
        ],
        [{"key": "one", "title": "夹带执行", "kind": "analysis", "arguments": {"command": "unsafe"}}],
    ],
)
def test_invalid_plans_cannot_enter_execution(steps: list[dict[str, object]]) -> None:
    """拒绝不可能完成或越过工具白名单的计划。

    Args:
        steps: 不可信模型生成的步骤数组。
    """
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({"steps": steps})


def test_plan_supports_dependencies_without_requiring_definition_order() -> None:
    """依赖图按就绪条件执行，而非误把数组排列视为完成顺序。"""
    plan = PlanDraft.model_validate(
        {
            "steps": [
                {"key": "report", "title": "汇总", "kind": "synthesis", "depends_on": ["collect"]},
                {
                    "key": "collect",
                    "title": "统计",
                    "kind": "tool",
                    "tool_name": "get_my_run_stats",
                    "arguments": {"days": 7},
                },
            ]
        }
    )
    assert plan.steps[0].depends_on == ["collect"]
    assert plan.steps[1].tool_name == "get_my_run_stats"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (408, True),
        (429, True),
        (502, True),
        (503, True),
        (504, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
    ],
)
def test_http_failure_classification(status: int, retryable: bool) -> None:
    """只对明确的暂时错误重试，不用异常正文推测。

    Args:
        status: 供应商状态码。
        retryable: 预期是否允许网络重试。
    """
    from app.services.task_retry import classify_error

    request = httpx.Request("POST", "https://provider.invalid/model")
    error = httpx.HTTPStatusError("private-response", request=request, response=httpx.Response(status, request=request))
    decision = classify_error(error)
    assert decision.retryable is retryable
    assert "private" not in decision.code


def test_quota_failure_is_not_treated_as_temporary_rate_limit() -> None:
    """额度不足即使返回429，也不应无限等待或自动重试。"""
    from app.services.task_retry import classify_error

    request = httpx.Request("POST", "https://provider.invalid/model")
    response = httpx.Response(429, request=request, json={"error": {"code": "insufficient_quota"}})
    decision = classify_error(httpx.HTTPStatusError("private-body", request=request, response=response))
    assert not decision.retryable


def test_retry_after_supports_seconds_http_date_and_invalid_values() -> None:
    """服务端要求的等待必须准确保留，非法值交给本地退避。"""
    from app.services.task_retry import parse_retry_after

    now = datetime(2026, 11, 4, 8, tzinfo=UTC)
    assert parse_retry_after("12", now) == 12
    assert parse_retry_after(format_datetime(now + timedelta(seconds=20), usegmt=True), now) == 20
    assert parse_retry_after(format_datetime(now - timedelta(seconds=20), usegmt=True), now) == 0
    assert parse_retry_after("garbage", now) is None
    assert parse_retry_after("-3", now) is None
    assert parse_retry_after("600", now) == 600


def test_backoff_is_bounded_and_does_not_shorten_retry_after() -> None:
    """抖动受上限约束，Retry-After可以超过普通退避上限。"""
    from app.services.task_retry import classify_error, retry_delay

    request = httpx.Request("POST", "https://provider.invalid/model")
    response = httpx.Response(503, request=request, headers={"Retry-After": "120"})
    decision = classify_error(httpx.HTTPStatusError("private-response", request=request, response=response))
    assert retry_delay(decision, 1, 2, 30) >= 120
    ordinary = classify_error(httpx.ConnectError("connection lost", request=request))
    for attempt in (1, 2, 10):
        assert 0 <= retry_delay(ordinary, attempt, 2, 30) <= min(30, 2 * 2 ** (attempt - 1))
    assert not classify_error(RuntimeError("unknown-private-error")).retryable
