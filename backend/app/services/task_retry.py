"""集中处理网络异常与退避，避免 SDK、图和业务层重试相乘。"""

import random
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from app.core.errors import ApplicationError


class ErrorResponse(Protocol):
    """兼容供应商传输响应，不绑定其内部 HTTP 客户端版本。"""

    @property
    def headers(self) -> Mapping[str, str]:
        """返回错误响应头。"""
        ...

    def json(self) -> Any:
        """读取供应商错误码，仅用于分类。"""
        ...


RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
PERMANENT_PROVIDER_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "invalid_api_key",
        "account_deactivated",
        "insufficient_balance",
    }
)


@dataclass(frozen=True)
class RetryDecision:
    """只传递公开错误类别，不保留异常敏感正文。"""

    code: str
    retryable: bool
    retry_after: float | None = None


def parse_retry_after(value: str, now: datetime | None = None) -> float | None:
    """解析服务端秒数或 HTTP 日期，不截短有效等待。

    Args:
        value: Retry-After 请求头。
        now: 测试可注入的当前 UTC 时间。

    Returns:
        非负等待秒数，无效时为空。
    """
    try:
        seconds = float(value)
        if seconds < 0 or not seconds < float("inf"):
            return None
        return seconds
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return max(0.0, (timestamp - (now or datetime.now(UTC))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def classify_error(exc: Exception) -> RetryDecision:
    """明确区分网络瞬断、限流与永久错误，未知错误不自动重放。

    Args:
        exc: 外部请求异常。

    Returns:
        集中重试决策。
    """
    if isinstance(exc, ApplicationError):
        return RetryDecision(exc.code, exc.code in {"TOOL_TIMEOUT", "NETWORK_TIMEOUT"})
    cause: BaseException | None = exc
    for _ in range(5):
        if isinstance(cause, ssl.SSLCertVerificationError):
            return RetryDecision("TLS_CONFIGURATION_ERROR", False)
        if isinstance(cause, socket.gaierror) and cause.errno == socket.EAI_NONAME:
            return RetryDecision("ENDPOINT_CONFIGURATION_ERROR", False)
        cause = cause.__cause__ if cause else None
    status: int | None = None
    response: ErrorResponse | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        status, response = exc.response.status_code, exc.response
    else:
        # 供应商 SDK 延迟导入，健康检查不承担模型 SDK 冷启动成本。
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(exc, APIStatusError):
            if exc.code in PERMANENT_PROVIDER_CODES:
                return RetryDecision("MODEL_ACCOUNT_ERROR", False)
            status, response = exc.status_code, exc.response
        elif isinstance(exc, (APIConnectionError, APITimeoutError)):
            return RetryDecision("NETWORK_TIMEOUT" if isinstance(exc, APITimeoutError) else "NETWORK_UNAVAILABLE", True)
    if status is not None:
        if status in (401, 403):
            return RetryDecision("MODEL_AUTH_DENIED", False)
        if status == 429 and response is not None:
            try:
                body = response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(error, dict) and error.get("code") in PERMANENT_PROVIDER_CODES:
                    return RetryDecision("MODEL_ACCOUNT_ERROR", False)
            except ValueError:
                pass
        after = parse_retry_after(response.headers.get("Retry-After", "")) if response is not None else None
        return RetryDecision("RATE_LIMITED" if status == 429 else f"HTTP_{status}", status in RETRYABLE_STATUS, after)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return RetryDecision("NETWORK_TIMEOUT", True)
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return RetryDecision("NETWORK_UNAVAILABLE", True)
    return RetryDecision("TASK_ACTION_FAILED", False)


def retry_delay(decision: RetryDecision, attempt_no: int, base: float, cap: float) -> float:
    """以实际请求次数计算退避，服务端有效等待优先。

    Args:
        decision: 已分类的异常。
        attempt_no: 已发生的请求次数，包含首次。
        base: 指数退避基数。
        cap: 随机退避上限。

    Returns:
        本次等待秒数。
    """
    if decision.retry_after is not None:
        return decision.retry_after
    return random.uniform(0, min(cap, base * 2 ** max(0, attempt_no - 1)))
