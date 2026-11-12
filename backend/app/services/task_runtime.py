"""本地持久化任务调度，网络等待和进程恢复均以数据库为准。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agents.task_provider import TaskProvider
from app.core.config import Settings
from app.core.database import Database
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope
from app.schemas.task import PlanDraft, PlanStep, TaskSnapshot
from app.schemas.tools import RunStatsArgs
from app.services.read_tools import query_run_stats
from app.services.task_retry import classify_error, retry_delay
from app.services.task_store import TaskStore

logger = logging.getLogger(__name__)
SCHEDULER_LOCK = 782341915
ACCESS_POLL_SECONDS = 1.0


class TaskRuntime:
    """只负责调度和外部等待；每次持久化调用使用独立短事务。"""

    def __init__(self, database: Database, settings: Settings, provider: TaskProvider) -> None:
        """注入依赖，不在构造时连接数据库或模型。

        Args:
            database: 应用连接池。
            settings: 调度和重试限制。
            provider: 可独立替换的计划/步骤模型。
        """
        self.database, self.settings, self.provider = database, settings, provider
        self.store = TaskStore(database.session_factory, settings.task_retry_attempts)
        self.owner = uuid4().hex
        self.jobs: dict[UUID, asyncio.Task[None]] = {}
        self.job_runs: dict[UUID, UUID] = {}
        self.job_tokens: dict[UUID, int] = {}
        self.scheduler: asyncio.Task[None] | None = None
        self.wakeup = asyncio.Event()
        self.closing = False
        self.lease: AsyncConnection | None = None

    async def start(self) -> None:
        """应用生命周期启动后台扫描，健康检查不依赖数据库可用。"""
        if self.settings.task_scheduler_enabled and self.scheduler is None:
            self.scheduler = asyncio.create_task(self._schedule())

    def poke(self) -> None:
        """创建或控制请求提交后唤醒扫描，不把事件本身当作事实来源。"""
        self.wakeup.set()

    async def interrupt(self, task_id: UUID, run_id: UUID | None = None, before_token: int | None = None) -> None:
        """数据库先提交控制状态，再取消本进程的飞行请求。

        Args:
            task_id: 用户已经控制的任务。
            run_id: 控制针对的执行，不能误停新建执行。
            before_token: 只停止此 fencing token 之前的旧执行器。
        """
        job = self.jobs.get(task_id)
        matches = run_id is None or self.job_runs.get(task_id) == run_id
        older = before_token is None or self.job_tokens.get(task_id, before_token) < before_token
        if job is not None and matches and older:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.poke()

    async def _tick(self, seconds: float) -> None:
        """可被控制操作唤醒的等待，不占用数据库连接。

        Args:
            seconds: 最大等待秒数。
        """
        try:
            await asyncio.wait_for(self.wakeup.wait(), timeout=max(0.01, seconds))
        except TimeoutError:
            pass
        self.wakeup.clear()

    async def _schedule(self) -> None:
        """持有单进程调度锁，数据库恢复后继续扫描到期记录。"""
        while not self.closing:
            try:
                if self.lease is None:
                    connection = await self.database.engine.connect()
                    try:
                        acquired = await connection.scalar(
                            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULER_LOCK}
                        )
                        await connection.commit()
                        if acquired:
                            self.lease = connection
                        else:
                            await connection.close()
                            await self._tick(self.settings.task_poll_seconds)
                            continue
                    except BaseException:
                        await connection.close()
                        raise
                for task_id, job in list(self.jobs.items()):
                    if job.done():
                        if not job.cancelled() and job.exception() is not None:
                            logger.error(
                                "task_executor_failed",
                                extra={"task_id": str(task_id), "error_type": type(job.exception()).__name__},
                            )
                        self.jobs.pop(task_id, None)
                        self.job_runs.pop(task_id, None)
                        self.job_tokens.pop(task_id, None)
                if len(self.jobs) < self.settings.task_concurrent_runs:
                    snapshot = await self.store.claim(self.owner)
                    if snapshot is not None:
                        previous = self.jobs.pop(snapshot.task.id, None)
                        if previous is not None:
                            previous.cancel()
                            await asyncio.gather(previous, return_exceptions=True)
                        self.job_runs[snapshot.task.id] = snapshot.run.id
                        self.job_tokens[snapshot.task.id] = snapshot.run.fencing_token
                        self.jobs[snapshot.task.id] = asyncio.create_task(self._supervise(snapshot))
                        continue
                await self._tick(self.settings.task_poll_seconds)
            except asyncio.CancelledError:
                return
            except (SQLAlchemyError, OSError, TimeoutError) as exc:
                logger.warning("task_scheduler_unavailable", extra={"error_type": type(exc).__name__})
                if self.lease is not None:
                    await self.lease.close()
                    self.lease = None
                await self._tick(max(2.0, self.settings.task_poll_seconds))

    async def _monitor(self, snapshot: TaskSnapshot) -> None:
        """没有模型输出和重试等待时也持续复核权限与截止时间。

        Args:
            snapshot: 本次领取的任务与 token。
        """
        elapsed = 0.0
        while True:
            await self.store.check(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token)
            elapsed += ACCESS_POLL_SECONDS
            if elapsed >= self.settings.task_heartbeat_seconds:
                await self.store.heartbeat(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, self.owner)
                elapsed = 0.0
            await asyncio.sleep(ACCESS_POLL_SECONDS)

    async def _supervise(self, snapshot: TaskSnapshot) -> None:
        """生成和权限监视任一失败都取消另一方，终态由账本提交。

        Args:
            snapshot: 新租约对应的任务快照。
        """
        tasks = [asyncio.create_task(self._execute(snapshot)), asyncio.create_task(self._monitor(snapshot))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            pass
        except SQLAlchemyError as exc:
            # 提交确认可能丢失，恢复时先对账，不能把整个任务直接重放。
            logger.warning(
                "task_persistence_unavailable",
                extra={"task_id": str(snapshot.task.id), "error_type": type(exc).__name__},
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, ApplicationError) else "TASK_EXECUTION_FAILED"
            if code not in ("TASK_LEASE_LOST", "TASK_PAUSED"):
                try:
                    await self.store.fail(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, code)
                except (ApplicationError, SQLAlchemyError):
                    logger.info("task_result_superseded", extra={"task_id": str(snapshot.task.id)})
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.store.release(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, self.owner)
            except (ApplicationError, SQLAlchemyError):
                logger.warning("task_release_deferred", extra={"task_id": str(snapshot.task.id)})
            self.poke()

    async def _action(
        self,
        snapshot: TaskSnapshot,
        key: str | None,
        kind: str,
        inputs: dict[str, object],
        operation: Callable[[], Awaitable[str]],
    ) -> str:
        """只重试一个模型/只读工具请求，先复用已提交的动作检查点。

        Args:
            snapshot: 当前任务和执行权。
            key: 步骤标识，规划为空。
            kind: plan/model/tool。
            inputs: 已验证的不可变输入快照。
            operation: 一次真实外部调用。

        Returns:
            已提交的完整结果。
        """
        task_id, run_id, token = snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token
        while True:
            action = await self.store.start_action(task_id, run_id, token, key, kind, inputs)
            if action.status == "succeeded" and action.result is not None:
                return action.result
            if action.status == "failed":
                raise ApplicationError(
                    action.error_code or "TASK_ACTION_FAILED", "上次请求已永久失败，不能自动重发", 502
                )
            if action.next_retry_at is not None:
                remaining = (action.next_retry_at - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, ACCESS_POLL_SECONDS))
                    continue
            if action.attempt_count >= self.settings.task_retry_attempts:
                raise ApplicationError("TASK_RETRY_EXHAUSTED", "本次请求的尝试次数已达上限", 502)
            attempt_id = await self.store.action_attempt(
                task_id, run_id, token, action.id, self.settings.task_retry_attempts
            )
            try:
                timeout = self.settings.tool_timeout_seconds if kind == "tool" else self.settings.model_timeout_seconds
                async with asyncio.timeout(timeout):
                    result = await operation()
                if not result.strip() or len(result) > self.settings.model_output_characters:
                    raise ApplicationError("INVALID_STEP_OUTPUT", "步骤未返回有效且完整的结果", 422)
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError:
                raise
            except Exception as exc:
                decision = classify_error(exc)
                count = action.attempt_count + 1
                if not decision.retryable or count >= self.settings.task_retry_attempts:
                    await self.store.finish_action(task_id, run_id, token, action.id, attempt_id, None, decision.code)
                    raise ApplicationError(decision.code, "步骤请求失败，请查看执行记录", 502) from exc
                wait = retry_delay(
                    decision, count, self.settings.task_retry_base_seconds, self.settings.task_retry_max_seconds
                )
                retry_at = datetime.now(UTC) + timedelta(seconds=wait)
                if snapshot.run.deadline_at is not None and retry_at >= snapshot.run.deadline_at:
                    await self.store.finish_action(
                        task_id, run_id, token, action.id, attempt_id, None, "DEADLINE_EXCEEDED"
                    )
                    raise ApplicationError("DEADLINE_EXCEEDED", "等待时间超过任务截止时间", 409) from exc
                long_wait = wait > self.settings.task_retry_max_wait_seconds
                await self.store.finish_action(
                    task_id, run_id, token, action.id, attempt_id, None, decision.code, retry_at, long_wait
                )
                if long_wait:
                    raise ApplicationError("TASK_PAUSED", "服务端要求较长等待，任务已暂停", 409) from exc
                continue
            # 结果提交在异常分类之外，避免把提交丢失误认为需要重新请求模型。
            await self.store.finish_action(task_id, run_id, token, action.id, attempt_id, result, None)
            return result

    async def _execute(self, snapshot: TaskSnapshot) -> None:
        """从已确认依赖图推进，只有全部必要步骤成功才汇总完成。

        Args:
            snapshot: 当前领取任务。
        """
        task_id, run_id, token = snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token
        if not snapshot.steps:

            async def generate_plan() -> str:
                """计划只描述能力，确认前不执行工具。"""
                plan = await self.provider.plan(snapshot.task.goal)
                return PlanDraft.model_validate(plan.model_dump()).model_dump_json()

            encoded = await self._action(snapshot, None, "plan", {"goal": snapshot.task.goal}, generate_plan)
            await self.store.save_plan(task_id, run_id, token, PlanDraft.model_validate_json(encoded))
            return
        while True:
            latest = await self.store.system_get(task_id)
            await self.store.check(task_id, run_id, token)
            if all(step.status == "succeeded" for step in latest.steps):
                depended = {key for step in latest.steps for key in step.depends_on}
                leaves = [step for step in latest.steps if step.key not in depended]
                result = "\n\n".join(f"{step.title}\n{step.result}" for step in leaves)
                await self.store.finish(task_id, run_id, token, result)
                return
            completed = {step.key for step in latest.steps if step.status == "succeeded"}
            ready = [
                step
                for step in latest.steps
                if step.status in ("pending", "retry_wait") and set(step.depends_on) <= completed
            ]
            if not ready or latest.plan is None:
                raise ApplicationError("TASK_DEPENDENCY_INVALID", "任务步骤无法继续推进", 409)
            now = datetime.now(UTC)
            due = [step for step in ready if step.next_retry_at is None or step.next_retry_at <= now]
            if not due:
                await asyncio.sleep(ACCESS_POLL_SECONDS)
                continue
            definitions = {step.key: step for step in latest.plan.steps}
            selected = due[: self.settings.task_parallel_steps]
            jobs = [
                asyncio.create_task(
                    self._step(
                        snapshot,
                        definitions[item.key],
                        {step.key: step.result or "" for step in latest.steps if step.key in item.depends_on},
                    )
                )
                for item in selected
            ]
            try:
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_EXCEPTION)
                for job in done:
                    await job
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def _step(self, snapshot: TaskSnapshot, step: PlanStep, inputs: dict[str, str]) -> None:
        """步骤输出基于实际依赖，工具查询仍采用后端绑定身份。

        Args:
            snapshot: 任务执行快照。
            step: 用户确认的步骤定义。
            inputs: 已提交的上游结果。
        """
        import json

        await self.store.start_step(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, step.key)
        if len(json.dumps(inputs, ensure_ascii=False)) > self.settings.model_context_characters:
            raise ApplicationError("TASK_CONTEXT_TOO_LONG", "依赖结果超过上下文限制，请缩小任务", 422)

        async def operation() -> str:
            """每次调用只执行当前步骤的单一真实能力。"""
            if step.kind == "tool":
                if not self.settings.tools_enabled or step.tool_name != "get_my_run_stats":
                    raise ApplicationError("TOOL_NOT_ALLOWED", "当前工具未获授权", 403)
                scope = ResourceScope(snapshot.task.owner_user_id, snapshot.task.workspace_id)
                result = await query_run_stats(
                    self.database.session_factory, scope, RunStatsArgs.model_validate(step.arguments)
                )
                return result.model_dump_json()
            return await self.provider.execute(snapshot.task.goal, step, inputs)

        result = await self._action(
            snapshot,
            step.key,
            "tool" if step.kind == "tool" else "model",
            {
                "goal": snapshot.task.goal,
                "step": step.model_dump(mode="json"),
                "inputs": inputs,
            },
            operation,
        )
        await self.store.complete_step(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, step.key, result)

    async def close(self) -> None:
        """退出只释放本进程执行权，已持久化计划和结果仍可恢复。"""
        self.closing = True
        if self.scheduler is not None:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
            self.scheduler = None
        for job in self.jobs.values():
            job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        self.jobs.clear()
        self.job_runs.clear()
        self.job_tokens.clear()
        if self.lease is not None:
            try:
                await self.lease.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEDULER_LOCK})
                await self.lease.commit()
            finally:
                await self.lease.close()
                self.lease = None
