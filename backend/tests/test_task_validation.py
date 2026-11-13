"""任务计划与网络重试的纯单元契约，不调用数据库或模型。"""

import socket
import ssl
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import ValidationError

from app.core.errors import ApplicationError
from app.schemas.task import PlanDraft, PlanStep, TaskCreate
from app.services.task_retry import RetryDecision, classify_error, parse_retry_after, retry_delay


def step(key: str, depends_on: list[str] | None = None) -> dict[str, object]:
    """构造无外部副作用的计划步骤。

    Args:
        key: 稳定步骤标识。
        depends_on: 上游依赖。

    Returns:
        供契约测试验证的定义。
    """
    return {"key": key, "title": "分析步骤", "kind": "analysis", "depends_on": depends_on or []}


def test_dag_accepts_independent_branches_and_reverse_definition_order() -> None:
    """执行顺序取决于依赖，而非模型给出的数组先后顺序。"""
    plan = PlanDraft.model_validate({"steps": [step("merge", ["a", "b"]), step("a"), step("b")]})
    assert plan.steps[0].depends_on == ["a", "b"]


@pytest.mark.parametrize(
    "steps",
    [
        [step("a"), step("a")],
        [step("a", ["missing"])],
        [step("a", ["a"])],
        [step("a", ["b"]), step("b", ["a"])],
        [step("a"), step("b", ["a", "a"])],
        [],
        [step("step" + str(index)) for index in range(9)],
    ],
)
def test_plan_rejects_invalid_dependencies_and_unbounded_steps(steps: list[dict[str, object]]) -> None:
    """错误计划不能进入用户确认和实际执行。

    Args:
        steps: 环、越界或重复的步骤定义。
    """
    with pytest.raises(ValidationError):
        PlanDraft.model_validate({"steps": steps})


@pytest.mark.parametrize(
    "changes",
    [
        {"tool_name": "execute"},
        {"tool_name": "get_my_run_stats", "kind": "analysis"},
        {"arguments": {"days": 7}, "kind": "synthesis"},
        {"arguments": {"days": "7"}},
        {"arguments": {"days": 91}},
        {"arguments": {"user_id": "spoof"}},
        {"arguments": {"workspace_id": "spoof"}},
        {"title": "   "},
    ],
)
def test_plan_rejects_capability_spoofing(changes: dict[str, object]) -> None:
    """模型不能夹带身份、未知工具或未验证参数。

    Args:
        changes: 不可信能力定义。
    """
    base = {"key": "stats", "title": "统计", "kind": "tool", "tool_name": "get_my_run_stats", "arguments": {"days": 7}}
    with pytest.raises(ValidationError):
        PlanStep.model_validate({**base, **changes})


def test_explicit_task_input_rejects_client_ownership() -> None:
    """可信身份只来自后端会话，不接受模型或客户端提供的所有者。"""
    with pytest.raises(ValidationError):
        TaskCreate.model_validate({"goal": "统计", "idempotency_key": "task-key", "owner_user_id": "spoof"})


@pytest.mark.parametrize(
    "value,expected", [("0", 0), ("120", 120), ("-1", None), ("nan", None), ("inf", None), ("nonsense", None)]
)
def test_retry_after_seconds_and_invalid_values(value: str, expected: float | None) -> None:
    """有效等待不截短，无效提示交给本地退避策略。

    Args:
        value: 响应头内容。
        expected: 解析秒数。
    """
    assert parse_retry_after(value) == expected


def test_retry_after_http_date_and_past_date() -> None:
    """HTTP 日期基于注入时钟，不依赖真实时间或长时间等待。"""
    now = datetime(2026, 11, 4, tzinfo=UTC)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=90), usegmt=True), now) == 90
    assert parse_retry_after(format_datetime(now - timedelta(seconds=20), usegmt=True), now) == 0


@pytest.mark.parametrize(
    "status,retryable",
    [
        (408, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        (401, False),
        (403, False),
        (400, False),
        (404, False),
    ],
)
def test_http_failure_classification(status: int, retryable: bool) -> None:
    """暂时性候选和永久错误明确区分，不能无条件重放所有错误。

    Args:
        status: HTTP 状态。
        retryable: 是否允许有限重试。
    """
    request = httpx.Request("GET", "https://provider.invalid/test")
    response = httpx.Response(status, request=request, headers={"Retry-After": "15"})
    decision = classify_error(httpx.HTTPStatusError("private-response", request=request, response=response))
    assert decision.retryable is retryable
    assert "private" not in decision.code
    if retryable:
        assert decision.retry_after == 15


def test_quota_errors_do_not_retry_even_when_http_status_is_429() -> None:
    """账号额度失败不能被误判为等待后会自动恢复的限流。"""
    request = httpx.Request("POST", "https://provider.invalid/test")
    response = httpx.Response(429, request=request, json={"error": {"code": "insufficient_quota"}})
    decision = classify_error(httpx.HTTPStatusError("private", request=request, response=response))
    assert decision.code == "MODEL_ACCOUNT_ERROR" and not decision.retryable


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        httpx.ReadTimeout("private"),
        httpx.ConnectError("private"),
        httpx.ReadError("private"),
        httpx.RemoteProtocolError("private"),
    ],
)
def test_transient_transport_errors_allow_bounded_retry(error: Exception) -> None:
    """网络类型可重试，不把错误正文写入公开决策。

    Args:
        error: 模拟外部传输异常。
    """
    decision = classify_error(error)
    assert decision.retryable and "private" not in decision.code


def test_permanent_configuration_and_permission_errors_do_not_retry() -> None:
    """证书、确定不存在的域名与授权错误需要修复而非等待。"""
    for cause in (ssl.SSLCertVerificationError("private"), socket.gaierror(socket.EAI_NONAME, "private")):
        error = httpx.ConnectError("private")
        error.__cause__ = cause
        assert not classify_error(error).retryable
    assert not classify_error(ApplicationError("ACCESS_REVOKED", "失效", 403)).retryable
    assert not classify_error(RuntimeError("private")).retryable


def test_retry_delay_uses_server_hint_and_bounded_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """有效 Retry-After 不受本地上限截短，随机窗口可重复验证。

    Args:
        monkeypatch: 注入确定的随机数。
    """
    assert retry_delay(RetryDecision("RATE_LIMITED", True, 600), 1, 2, 30) == 600
    monkeypatch.setattr("app.services.task_retry.random.uniform", lambda low, high: high)
    decision = RetryDecision("NETWORK_TIMEOUT", True)
    assert retry_delay(decision, 1, 2, 30) == 2
    assert retry_delay(decision, 2, 2, 30) == 4
    assert retry_delay(decision, 20, 2, 30) == 30
