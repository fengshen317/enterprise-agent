"""按可信工作空间采集、写入及查询结构化诊断事件。"""

from __future__ import annotations

import asyncio
import logging
import re
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, delete, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.models.system_log import SystemLogEvent
from app.schemas.system_log import (
    Component,
    Outcome,
    SystemLogCounts,
    SystemLogDetail,
    SystemLogFilters,
    SystemLogPage,
    SystemLogView,
)

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
QUEUE_CAPACITY = 1024
BATCH_SIZE = 64
BATCH_WAIT_SECONDS = 0.03
DB_TIMEOUT_SECONDS = 5
SHUTDOWN_TIMEOUT_SECONDS = 2
RETENTION_INTERVAL_SECONDS = 3600
RETENTION_BATCH_SIZE = 1000
RELATED_LIMIT = 100

_EVENT_COMPONENTS: dict[str, set[Component]] = {
    "http_request_accepted": {"http"},
    "http_request_failed": {"http"},
    "run_started": {"run"},
    "run_finished": {"run"},
    "tool_finished": {"tool", "connector"},
    "model_retry": {"model"},
    "model_finished": {"model"},
    "connector_check": {"connector"},
    "schedule_finished": {"schedule"},
    "wechat_delivery": {"wechat"},
}
_LEVELS = {
    "success": ("info", logging.INFO),
    "warning": ("warning", logging.WARNING),
    "error": ("error", logging.ERROR),
}
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_CONNECTOR_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TOOL_NAME = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,127}")
_ROUTE_SEGMENTS = frozenset(
    """
    action activity admin agent agents approvals archive audits auth bind cancel channels check chunks configs config
    confirm connectors connectors-builtin conversations decide delete documents enabled events file health holdings ima
    install knowledge-bases knowledge-documents live login logout me members messages my-account plan parse publish
    ready reparse-documents retrieval-test run runs schedules skills status system-logs tasks test tool-calls tools
    trace unbind versions wechat wechat-push workspaces yangjibao
    {account_id} {action} {agent_id} {approval_id} {conversation_id} {document_id} {event_id} {id} {kb_id} {key}
    {run_id} {schedule_id} {session_id} {skill_id} {task_id} {user_id}
    """.split()
)
_DETAIL_ENUMS = {
    "method": frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}),
    "phase": frozenset({"request_validation", "auth", "transport", "upstream_business", "response_validation"}),
    "rule": frozenset(
        {
            "invalid_json",
            "response_envelope",
            "response_data",
            "knowledge_base_id",
            "knowledge_list",
            "row_identity",
            "media_type",
            "note_identity",
            "note_content",
        }
    ),
    "run_status": frozenset({"completed", "failed", "cancelled"}),
    "channel": frozenset({"wechat"}),
}
_DETAIL_RANGES = {
    "status_code": (100, 599),
    "upstream_status": (100, 599),
    "upstream_code": (-999_999, 999_999),
    "attempt": (1, 100),
    "max_attempts": (1, 100),
}
_CONNECTOR_DETAILS = {"phase", "upstream_status", "upstream_code", "rule"}
_HTTP_DETAILS = {"route", "method", "status_code"}
_DETAIL_FIELDS = {
    "http_request_accepted": _HTTP_DETAILS,
    "http_request_failed": _HTTP_DETAILS,
    "run_finished": {"run_status", "channel"},
    "run_started": {"channel"},
    "tool_finished": _CONNECTOR_DETAILS,
    "model_retry": {"attempt", "max_attempts", "upstream_status", "phase"},
    "model_finished": {"attempt", "max_attempts", "upstream_status", "phase"},
    "connector_check": _CONNECTOR_DETAILS,
    "schedule_finished": {"run_status", "channel"},
    "wechat_delivery": {"channel", "upstream_status"},
}


class _ContextDefault(Enum):
    """区分缺省继承与调用处显式清理。"""

    inherit = "inherit"


_INHERIT: Final = _ContextDefault.inherit


@dataclass(frozen=True, slots=True)
class _LogScope:
    """一次业务调用可继承的记录器和可信关联。"""

    recorder: SystemLogRecorder | None = None
    workspace_id: UUID | None = None
    request_id: str | None = None
    run_id: UUID | None = None


_EMPTY_SCOPE: Final = _LogScope()
_scope: ContextVar[_LogScope] = ContextVar("system_log_scope", default=_EMPTY_SCOPE)


def _uuid(value: UUID | str | None) -> UUID | None:
    """接受标准 UUID 或完整十六进制标识，其他输入舍弃。"""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and len(value) in {32, 36}:
        with suppress(ValueError):
            return UUID(value)
    return None


@contextmanager
def system_log_context(
    recorder: SystemLogRecorder | None | Literal[_ContextDefault.inherit] = _INHERIT,
    *,
    workspace_id: UUID | None | Literal[_ContextDefault.inherit] = _INHERIT,
    request_id: UUID | str | None | Literal[_ContextDefault.inherit] = _INHERIT,
    run_id: UUID | None | Literal[_ContextDefault.inherit] = _INHERIT,
) -> Iterator[None]:
    """在服务端已验证的工作空间中传播诊断关联。

    Args:
        recorder: 应用所属的记录器。
        workspace_id: 可信空间；切换空间时自动清理缺省关联。
        request_id: 本服务产生的请求标识。
        run_id: 已关联的运行。

    Yields:
        当前调用作用域，显式传入 None 时清理字段。
    """
    previous = _scope.get()
    space = previous.workspace_id if workspace_id is _INHERIT else workspace_id
    same_scope = previous.workspace_id is None or space == previous.workspace_id
    if request_id is _INHERIT:
        request = previous.request_id if same_scope else None
    else:
        parsed = _uuid(request_id)
        request = parsed.hex if parsed is not None else None
    if run_id is _INHERIT:
        run_id = previous.run_id if same_scope else None
    token = _scope.set(_LogScope(previous.recorder if recorder is _INHERIT else recorder, space, request, run_id))
    try:
        yield
    finally:
        _scope.reset(token)


def _clean_details(event: str, details: Mapping[str, object] | None) -> dict[str, str | int]:
    """按事件与值域提取有限字段，丢弃自由文本和嵌套对象。"""
    clean: dict[str, str | int] = {}
    if not details:
        return clean
    for key in _DETAIL_FIELDS.get(event, ()):
        value = details.get(key)
        if type(value) is str and value in _DETAIL_ENUMS.get(key, ()):
            clean[key] = value
        elif type(value) is int and key in _DETAIL_RANGES:
            lower, upper = _DETAIL_RANGES[key]
            if lower <= value <= upper:
                clean[key] = value
        elif key == "route" and type(value) is str and value.startswith("/api/") and len(value) <= 120:
            if all(part in _ROUTE_SEGMENTS for part in value.removeprefix("/api/").split("/")):
                clean[key] = value
    return clean


def emit_system_log(
    *,
    component: Component,
    event: str,
    outcome: Outcome,
    workspace_id: UUID | None = None,
    request_id: UUID | str | None = None,
    run_id: UUID | None = None,
    tool_call_id: UUID | None = None,
    connector_key: str | None = None,
    tool_name: str | None = None,
    task_execution_id: UUID | None = None,
    error_code: str | None = None,
    duration_ms: float | None = None,
    details: Mapping[str, object] | None = None,
) -> UUID | None:
    """输出一个脱敏事件，有可信空间时加入独立写入队列。

    Args:
        component: 事件所属模块。
        event: 服务端预定义的事件类型。
        outcome: 执行结果，同时决定日志级别。
        workspace_id: 服务端已确认的空间，缺省继承上下文。
        request_id: 本服务产生的请求标识。
        run_id: 已关联的运行。
        tool_call_id: 工具调用标识。
        connector_key: 已解析的连接器标识。
        tool_name: 已解析的工具标识。
        task_execution_id: 调度执行标识。
        error_code: 服务端错误分类。
        duration_ms: 耗时毫秒。
        details: 只提取事件白名单允许的字段。

    Returns:
        该次事件的诊断编号，无法识别事件时为空。
    """
    if component not in _EVENT_COMPONENTS.get(event, ()) or outcome not in _LEVELS:
        logger.warning("system_log_invalid_event")
        return None
    scope = _scope.get()
    space = workspace_id if workspace_id is not None else scope.workspace_id
    if not isinstance(space, UUID):
        space = None
    same_scope = scope.workspace_id is None or space == scope.workspace_id
    request = _uuid(request_id)
    level, log_level = _LEVELS[outcome]
    identifier = uuid4()
    occurred_at = datetime.now(UTC)
    fields = {
        "level": level,
        "component": component,
        "event": event,
        "outcome": outcome,
        "error_code": error_code if error_code and _ERROR_CODE.fullmatch(error_code) else None,
        "duration_ms": int(duration_ms) if duration_ms is not None and 0 <= duration_ms <= 2_147_483_647 else None,
        "request_id": request.hex if request is not None else (scope.request_id if same_scope else None),
        "run_id": _uuid(run_id) if run_id is not None else (scope.run_id if same_scope else None),
        "tool_call_id": _uuid(tool_call_id),
        "connector_key": connector_key if connector_key and _CONNECTOR_KEY.fullmatch(connector_key) else None,
        "tool_name": tool_name if tool_name and _TOOL_NAME.fullmatch(tool_name) else None,
        "task_execution_id": _uuid(task_execution_id),
        "details": _clean_details(event, details),
    }
    payload = {
        **{key: str(value) if isinstance(value, UUID) else value for key, value in fields.items()},
        "id": str(identifier),
        "occurred_at": occurred_at.isoformat(),
        "workspace_id": str(space) if space is not None else None,
    }
    logger.log(log_level, "system_log_event", extra={"diagnostic": payload})
    if scope.recorder is not None and space is not None:
        scope.recorder.enqueue(SystemLogEvent(id=identifier, occurred_at=occurred_at, workspace_id=space, **fields))
    return identifier


class SystemLogRecorder:
    """将脱敏事件通过独立的后台会话批量写入 PostgreSQL。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        """初始化有界队列和保留期。

        Args:
            factory: 应用数据库的独立会话工厂。
            retention_days: 结构化事件保留天数。
        """
        self.factory = factory
        self.retention_days = retention_days
        self._queue: asyncio.Queue[SystemLogEvent] = asyncio.Queue(maxsize=QUEUE_CAPACITY)
        self._worker: asyncio.Task[None] | None = None
        self._janitor: asyncio.Task[None] | None = None
        self._accepting = False
        self.dropped_count = 0
        self.write_failure_count = 0
        self.retention_failure_count = 0

    async def start(self) -> None:
        """启动应用专属的写入和清理任务。"""
        if self._worker is not None:
            return
        self._accepting = True
        self._worker = asyncio.create_task(self._write(), name="system-log-writer")
        self._janitor = asyncio.create_task(self._maintain(), name="system-log-retention")

    def enqueue(self, event: SystemLogEvent) -> None:
        """尝试入队，队列满或写入器关闭时只累加丢弃计数。"""
        if not self._accepting:
            self._discard(1, "closed")
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._discard(1, "queue_full")

    def _discard(self, count: int, reason: str) -> None:
        """报告明显变化或单个批次失败，防止队列饱和时放大输出。"""
        previous = self.dropped_count
        self.dropped_count += count
        if reason not in {"queue_full", "closed"} or self.dropped_count.bit_length() > previous.bit_length():
            logger.warning(
                "system_log_events_dropped",
                extra={
                    "diagnostic": {
                        "reason": reason,
                        "dropped_count": self.dropped_count,
                        "write_failure_count": self.write_failure_count,
                    },
                },
            )

    async def _write(self) -> None:
        """批量提交，写入异常只影响该批诊断事件。"""
        while True:
            batch = [await self._queue.get()]
            try:
                if self._accepting:
                    await asyncio.sleep(BATCH_WAIT_SECONDS)
                while len(batch) < BATCH_SIZE and not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                async with asyncio.timeout(DB_TIMEOUT_SECONDS), self.factory.begin() as session:
                    session.add_all(batch)
            except asyncio.CancelledError:
                self._discard(len(batch), "shutdown_timeout")
                raise
            except Exception:
                self.write_failure_count += 1
                self._discard(len(batch), "write_failed")
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _maintain(self) -> None:
        """周期性清除过期事件，每批持有短事务。"""
        while True:
            try:
                await self.purge_expired()
            except Exception:
                self.retention_failure_count += 1
                logger.warning(
                    "system_log_retention_failed",
                    extra={"diagnostic": {"retention_failure_count": self.retention_failure_count}},
                )
            await asyncio.sleep(RETENTION_INTERVAL_SECONDS)

    async def purge_expired(self) -> int:
        """按数据库时间范围分批清理过期事件，返回删除数量。"""
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        expired = (
            select(SystemLogEvent.id)
            .where(SystemLogEvent.occurred_at < cutoff)
            .order_by(SystemLogEvent.occurred_at)
            .limit(RETENTION_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        removed = 0
        while True:
            async with asyncio.timeout(DB_TIMEOUT_SECONDS), self.factory.begin() as session:
                deleted = await session.scalars(
                    delete(SystemLogEvent).where(SystemLogEvent.id.in_(expired)).returning(SystemLogEvent.id)
                )
                count = len(deleted.all())
            removed += count
            if count < RETENTION_BATCH_SIZE:
                return removed

    async def close(self) -> None:
        """停止接受新事件，尝试限时提交剩余批次再取消后台任务。"""
        self._accepting = False
        if self._janitor is not None:
            self._janitor.cancel()
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT_SECONDS):
                await self._queue.join()
        except TimeoutError:
            pass
        finally:
            if self._worker is not None:
                self._worker.cancel()
            for task in (self._worker, self._janitor):
                if task is not None:
                    with suppress(asyncio.CancelledError):
                        await task
            self._worker = self._janitor = None
            discarded = self._queue.qsize()
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            if discarded:
                self._discard(discarded, "shutdown_timeout")


def _encode_cursor(row: SystemLogEvent) -> str:
    """将已返回行的时间和 ID 编为游标。"""
    value = f"{row.occurred_at.isoformat()}|{row.id.hex}"
    return urlsafe_b64encode(value.encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """解析完整、带时区的游标，拒绝非法值。"""
    try:
        decoded = b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True).decode("ascii")
        raw_time, raw_id = decoded.split("|")
        timestamp, identifier = datetime.fromisoformat(raw_time), UUID(hex=raw_id)
        if timestamp.utcoffset() is None:
            raise ValueError
        return timestamp, identifier
    except (ValueError, Base64Error, OverflowError):
        raise ApplicationError("SYSTEM_LOG_CURSOR_INVALID", "日志分页游标无效，请重新查询", 422) from None


async def list_system_logs(session: AsyncSession, workspace_id: UUID, filters: SystemLogFilters) -> SystemLogPage:
    """按空间和筛选参数查询事件，并计算各结果类别总数。

    Args:
        session: 本次只读查询的异步会话。
        workspace_id: 已确认具有管理权限的空间。
        filters: 已校验的分页和筛选参数。

    Returns:
        匹配的诊断事件、结果统计和下一页游标。
    """
    criteria = [
        SystemLogEvent.workspace_id == workspace_id,
        SystemLogEvent.occurred_at >= datetime.now(UTC) - timedelta(days=filters.days),
    ]
    for column, value in (
        (SystemLogEvent.component, filters.component),
        (SystemLogEvent.level, filters.level),
        (SystemLogEvent.connector_key, filters.connector_key),
        (SystemLogEvent.error_code, filters.error_code),
    ):
        if value is not None:
            criteria.append(column == value)
    query = (filters.q or "").strip()
    if identifier := _uuid(query):
        criteria.append(
            or_(
                SystemLogEvent.id == identifier,
                SystemLogEvent.request_id == identifier.hex,
                SystemLogEvent.run_id == identifier,
                SystemLogEvent.tool_call_id == identifier,
                SystemLogEvent.task_execution_id == identifier,
                SystemLogEvent.error_code == query,
            )
        )
    elif query:
        criteria.append(SystemLogEvent.error_code == query)
    cursor = _decode_cursor(filters.cursor) if filters.cursor is not None else None
    grouped = await session.execute(
        select(SystemLogEvent.outcome, func.count()).where(*criteria).group_by(SystemLogEvent.outcome)
    )
    counts = {outcome: count for outcome, count in grouped.tuples()}
    totals = SystemLogCounts(
        all=sum(counts.values()),
        success=counts.get("success", 0),
        warning=counts.get("warning", 0),
        error=counts.get("error", 0),
    )
    if filters.outcome is not None:
        criteria.append(SystemLogEvent.outcome == filters.outcome)
    if cursor is not None:
        criteria.append(tuple_(SystemLogEvent.occurred_at, SystemLogEvent.id) < tuple_(*map(literal, cursor)))
    rows = (
        await session.scalars(
            select(SystemLogEvent)
            .where(*criteria)
            .order_by(SystemLogEvent.occurred_at.desc(), SystemLogEvent.id.desc())
            .limit(filters.limit + 1)
        )
    ).all()
    page = rows[: filters.limit]
    return SystemLogPage(
        items=[SystemLogView.model_validate(row) for row in page],
        total=counts.get(filters.outcome, 0) if filters.outcome is not None else totals.all,
        counts=totals,
        next_cursor=_encode_cursor(page[-1]) if len(rows) > filters.limit else None,
    )


async def get_system_log(session: AsyncSession, workspace_id: UUID, event_id: UUID) -> SystemLogDetail:
    """读取工作空间内的事件，以及具有相同可信标识的其他记录。

    Args:
        session: 本次只读查询的异步会话。
        workspace_id: 已确认具有管理权限的空间。
        event_id: 诊断事件编号。

    Returns:
        主事件与时间顺序排列的关联记录。
    """
    in_workspace = SystemLogEvent.workspace_id == workspace_id
    event = await session.scalar(select(SystemLogEvent).where(in_workspace, SystemLogEvent.id == event_id))
    if event is None:
        raise ApplicationError("SYSTEM_LOG_NOT_FOUND", "未找到该诊断事件", 404)
    links: list[ColumnElement[bool]] = [
        column == value
        for column, value in (
            (SystemLogEvent.request_id, event.request_id),
            (SystemLogEvent.run_id, event.run_id),
            (SystemLogEvent.tool_call_id, event.tool_call_id),
            (SystemLogEvent.task_execution_id, event.task_execution_id),
        )
        if value is not None
    ]
    if not links:
        return SystemLogDetail(event=SystemLogView.model_validate(event), related=[], related_truncated=False)
    related = (
        await session.scalars(
            select(SystemLogEvent)
            .where(in_workspace, SystemLogEvent.id != event_id, or_(*links))
            .order_by(SystemLogEvent.occurred_at, SystemLogEvent.id)
            .limit(RELATED_LIMIT + 1)
        )
    ).all()
    return SystemLogDetail(
        event=SystemLogView.model_validate(event),
        related=[SystemLogView.model_validate(row) for row in related[:RELATED_LIMIT]],
        related_truncated=len(related) > RELATED_LIMIT,
    )
