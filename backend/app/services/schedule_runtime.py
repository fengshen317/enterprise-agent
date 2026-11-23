"""定时问答调度；复用单进程问答执行器，不另建生成链路。"""

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings
from app.core.database import Database
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope
from app.schemas.chat import RunCreate, RunSnapshot
from app.services.chat_runtime import ChatRuntime
from app.services.schedule_store import ClaimedSchedule, ScheduleStore
from app.services.system_logs import emit_system_log, system_log_context

logger = logging.getLogger(__name__)
SCHEDULE_LOCK = 782341917


class ScheduleRuntime:
    """持有单进程调度锁，到期后通过 ChatRuntime 创建并执行真实运行。"""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        chat_runtime: ChatRuntime,
        store: ScheduleStore | None = None,
    ) -> None:
        """绑定连接池、配置与问答执行器。

        Args:
            database: 应用连接池。
            settings: 调度开关、轮询间隔与默认时区。
            chat_runtime: 实际执行问答的运行管理器。
            store: 可选共享存储，供写工具与调度器复用。
        """
        self.database = database
        self.settings = settings
        self.chat_runtime = chat_runtime
        self.store = store or ScheduleStore(database.session_factory, settings.schedule_timezone)
        self.scheduler: asyncio.Task[None] | None = None
        self.wakeup = asyncio.Event()
        self.closing = False
        self.lease: AsyncConnection | None = None

    async def start(self) -> None:
        """在应用生命周期内启动轮询；测试和关闭状态不启动。"""
        if self.settings.schedule_enabled and self.scheduler is None:
            self.scheduler = asyncio.create_task(self._run())

    def poke(self) -> None:
        """创建或修改后唤醒扫描，事件本身不替代数据库状态。"""
        self.wakeup.set()

    async def _tick(self, seconds: float) -> None:
        """可被写操作唤醒的等待。

        Args:
            seconds: 最大等待秒数。
        """
        try:
            await asyncio.wait_for(self.wakeup.wait(), timeout=max(0.01, seconds))
        except TimeoutError:
            pass
        self.wakeup.clear()

    async def _run(self) -> None:
        """持有调度锁，数据库或问答进程不可用时等待后重试。"""
        while not self.closing:
            try:
                if self.lease is None:
                    connection = await self.database.engine.connect()
                    try:
                        acquired = await connection.scalar(
                            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULE_LOCK}
                        )
                        await connection.commit()
                        if not acquired:
                            await connection.close()
                            await self._tick(self.settings.schedule_poll_seconds)
                            continue
                        self.lease = connection
                    except BaseException:
                        await connection.close()
                        raise
                try:
                    await self.chat_runtime.ensure_ready()
                except ApplicationError as exc:
                    # 其他进程正持有问答锁或服务正在退出；不领取任务，稍后重试。
                    logger.warning("schedule_runtime_waiting", extra={"error_code": exc.code})
                    await self._tick(max(2.0, self.settings.schedule_poll_seconds))
                    continue
                if await self.run_due_once():
                    continue
                await self._tick(self.settings.schedule_poll_seconds)
            except asyncio.CancelledError:
                return
            except (SQLAlchemyError, OSError, TimeoutError) as exc:
                logger.warning("schedule_scheduler_unavailable", extra={"error_type": type(exc).__name__})
                if self.lease is not None:
                    await self.lease.close()
                    self.lease = None
                await self._tick(max(2.0, self.settings.schedule_poll_seconds))

    async def run_due_once(self) -> bool:
        """领取并执行一条到期任务，供调度循环与测试复用。

        Returns:
            是否实际领取到任务。
        """
        claimed = await self.store.claim_due(datetime.now(UTC))
        if claimed is None:
            return False
        await self._execute(claimed)
        return True

    async def _execute(self, claimed: ClaimedSchedule) -> None:
        """在固定对话中创建新运行；失败只记录跳过状态。

        Args:
            claimed: 已推进下一次触发的任务快照。
        """
        scope = ResourceScope(claimed.owner_user_id, claimed.workspace_id)
        started = perf_counter()
        error_code: str | None = None
        key = f"schedule_{claimed.schedule_id.hex}_{claimed.scheduled_for.strftime('%Y%m%d%H%M%S')}"
        try:
            self.chat_runtime.provider.validate()
            snapshot = await self.chat_runtime.create_run(
                claimed.conversation_id,
                RunCreate(content=claimed.prompt, idempotency_key=key),
                scope,
            )
            await self.store.record_started(claimed.schedule_id, snapshot.run.id, datetime.now(UTC))
            if snapshot.run.status == "pending":
                self.chat_runtime.schedule(snapshot.run.id, f"schedule-{claimed.schedule_id.hex[:8]}", scheduled=True)
            logger.info(
                "schedule_run_started",
                extra={"schedule_id": str(claimed.schedule_id), "run_id": str(snapshot.run.id)},
            )
        except ApplicationError as exc:
            error_code = exc.code
            await self.store.record_skipped(claimed.schedule_id, datetime.now(UTC), exc.code)
            logger.warning(
                "schedule_run_skipped",
                extra={"schedule_id": str(claimed.schedule_id), "error_code": exc.code},
            )
        except Exception as exc:
            error_code = "SCHEDULE_EXECUTION_FAILED"
            await self.store.record_skipped(claimed.schedule_id, datetime.now(UTC), "SCHEDULE_EXECUTION_FAILED")
            logger.error(
                "schedule_run_failed",
                extra={"schedule_id": str(claimed.schedule_id), "error_type": type(exc).__name__},
            )
        if error_code is not None:
            with system_log_context(
                recorder=self.chat_runtime.log_recorder, workspace_id=scope.workspace_id, request_id=None, run_id=None
            ):
                emit_system_log(
                    component="schedule",
                    event="schedule_finished",
                    outcome="warning" if error_code == "RUN_IN_PROGRESS" else "error",
                    task_execution_id=uuid4(),
                    error_code=error_code,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )

    async def run_now(self, scope: ResourceScope, schedule_id: UUID, *, request_id: str | None = None) -> RunSnapshot:
        """手动触发一次，不改变原定 Cron 的下一次时间。

        Args:
            scope: 已验证的当前用户和空间。
            schedule_id: 定时任务标识。
            request_id: 手动触发的可信 HTTP 请求编号。

        Returns:
            新建运行快照。

        Raises:
            ApplicationError: 任务不存在、对话正在生成或模型未配置。
        """
        view = await self.store.get(scope, schedule_id)
        # 手动触发也必须先获得问答执行权并收敛遗留运行，不能绕过单进程边界。
        await self.chat_runtime.ensure_ready()
        self.chat_runtime.provider.validate()
        key = f"manual_{schedule_id.hex}_{uuid4().hex}"
        snapshot = await self.chat_runtime.create_run(
            view.conversation_id,
            RunCreate(content=view.prompt, idempotency_key=key),
            scope,
        )
        await self.store.record_started(schedule_id, snapshot.run.id, datetime.now(UTC))
        if snapshot.run.status == "pending":
            self.chat_runtime.schedule(snapshot.run.id, request_id or f"manual-{schedule_id.hex[:8]}", scheduled=True)
        return snapshot

    async def close(self) -> None:
        """停止轮询并释放调度锁，不取消已提交的问答运行。"""
        self.closing = True
        if self.scheduler is not None:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
            self.scheduler = None
        if self.lease is not None:
            try:
                await self.lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEDULE_LOCK})
                await self.lease.commit()
            finally:
                await self.lease.close()
                self.lease = None
