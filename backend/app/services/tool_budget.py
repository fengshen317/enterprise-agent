"""工具预算与无进展检测；不保存查询条件、凭证或业务正文。"""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from secrets import token_bytes

from app.capabilities.spec import ToolSpec
from app.connectors.base import ConnectorError
from app.core.config import Settings
from app.core.errors import ApplicationError

BUDGET_CLOSING_NOTE = (
    "本轮查询已达到执行上限，以上根据已取得的资料整理。"
    "如需补查，请继续说明要查询的范围。"
)


@dataclass(frozen=True)
class ToolBudgetSnapshot:
    """一次模型请求前读取的共享预算，不包含工具原始数据。"""

    exhausted: frozenset[str]
    closing: bool
    business_calls: int
    model_calls: int


def effective_tool_limit(spec: ToolSpec, settings: Settings) -> int:
    """按显式类别分配预算，同时保留工具自身的更严格限制。"""
    if spec.system:
        return spec.quota_per_run
    category_limit = {
        "search": settings.tool_search_max_calls_per_run,
        "read": settings.tool_read_max_calls_per_run,
        "default": settings.tool_max_calls_per_run,
    }[spec.budget_kind]
    return min(spec.quota_per_run, category_limit, settings.tool_max_calls_per_run)


class ToolProgressGuard:
    """同一运行共享检测器，只串行化同工具、同参数的只读调用。"""

    def __init__(self, repeat_limit: int) -> None:
        """为本轮生成独立密钥，防止检测摘要被用于反推查询条件。"""
        self.repeat_limit = repeat_limit
        self._key = token_bytes(32)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._results: dict[tuple[str, str], tuple[str, int]] = {}

    def _digest(self, value: object) -> str:
        """仅保留带密钥的规范化摘要，不保留明文业务资料。"""
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def _record(self, path: tuple[str, str], outcome: object) -> None:
        """新结果重置当前路径的计数，相同结果才累计无进展次数。"""
        digest = self._digest(outcome)
        previous, count = self._results.get(path, ("", 0))
        self._results[path] = digest, count + 1 if digest == previous else 1

    async def execute(
        self, name: str, arguments: object, operation: Callable[[], Awaitable[object]]
    ) -> object:
        """执行只读动作，阻止连续返回相同结果或相同错误的查询路径。

        Args:
            name: 已授权的工具名。
            arguments: 参数模型规范化后的业务条件。
            operation: 包含权限复核与超时的实际执行动作。

        Returns:
            上游真实返回的业务结果。

        Raises:
            ApplicationError: 同一路径已连续达到无进展阈值。
        """
        path = name, self._digest(arguments)
        async with self._locks.setdefault(path, asyncio.Lock()):
            if self._results.get(path, ("", 0))[1] >= self.repeat_limit:
                raise ApplicationError(
                    "TOOL_NO_PROGRESS",
                    "相同查询连续多次没有带来新信息，请使用已有结果或更换查询条件，不要重复相同调用。",
                    422,
                )
            try:
                result = await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, (ConnectorError, ApplicationError)) else type(exc).__name__
                self._record(path, {"error": code})
                raise
            self._record(path, {"data": result})
            return result
