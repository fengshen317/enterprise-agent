"""任务短事务、身份隔离、租约和可重放执行账本。"""

from __future__ import annotations

import hashlib
import json
from builtins import list as ListType
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.models.conversation import Conversation
from app.models.task import Task, TaskAction, TaskActionAttempt, TaskEvent, TaskPlan, TaskRun, TaskStep, TaskStepAttempt
from app.schemas.auth import ResourceScope
from app.schemas.task import (
    ActionView,
    PlanDraft,
    PlanView,
    TaskActionSummary,
    TaskEventView,
    TaskRunView,
    TaskSnapshot,
    TaskStepView,
    TaskView,
)
from app.services.auth import require_membership

LEASE_SECONDS = 30
PLANNING_SECONDS = 300
EXECUTION_SECONDS = 1800
ACTIVE = ("pending", "planning", "running", "retry_wait")
TERMINAL = ("completed", "failed", "cancelled")
CREATE_LOCK = 782341914


class TaskStore:
    """任务行锁保护事件、控制状态与全部结果提交。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], max_attempts: int = 3) -> None:
        """保存独立会话工厂。

        Args:
            factory: 任务数据库会话工厂。
            max_attempts: 公开展示的单请求尝试上限。
        """
        self.factory = factory
        self.max_attempts = max_attempts

    async def _task(self, session: AsyncSession, task_id: UUID, scope: ResourceScope | None = None) -> Task:
        """按稳定锁顺序加载任务，外部访问必须同时匹配用户和空间。

        Args:
            session: 当前事务。
            task_id: 任务标识。
            scope: 外部可信身份；后台仅按任务标识读取。

        Returns:
            已锁定任务。
        """
        statement = select(Task).where(Task.id == task_id)
        if scope is not None:
            await require_membership(session, scope)
            statement = statement.where(Task.workspace_id == scope.workspace_id, Task.owner_user_id == scope.user_id)
        task = await session.scalar(statement.with_for_update())
        if task is None:
            raise ApplicationError("TASK_NOT_FOUND", "任务不存在", 404)
        return task

    async def _run(self, session: AsyncSession, task: Task) -> TaskRun:
        """当前运行必须确实属于该任务。

        Args:
            session: 当前事务。
            task: 已锁定任务。

        Returns:
            当前执行尝试。
        """
        run = await session.scalar(select(TaskRun).where(TaskRun.id == task.current_run_id, TaskRun.task_id == task.id))
        if run is None:
            raise ApplicationError("TASK_STATE_INVALID", "任务执行记录不完整", 409)
        return run

    async def _guard(
        self,
        session: AsyncSession,
        task_id: UUID,
        run_id: UUID,
        token: int,
        access: bool = True,
        deadline: bool = True,
    ) -> tuple[Task, TaskRun]:
        """提交前复核租约、取消状态、身份与截止时间。

        Args:
            session: 当前事务。
            task_id: 任务标识。
            run_id: 执行标识。
            token: 领取时的 fencing token。
            access: 是否要求有效成员，失败收敛可以关闭。
            deadline: 是否校验截止时间，失败收敛可以关闭。

        Returns:
            仍由调用者持有的任务与执行。
        """
        task = await self._task(session, task_id)
        run = await self._run(session, task)
        now = datetime.now(UTC)
        if run.id != run_id or run.fencing_token != token or run.status not in ACTIVE:
            raise ApplicationError("TASK_LEASE_LOST", "任务执行权已变化", 409)
        if run.lease_expires_at is None or run.lease_expires_at <= now:
            raise ApplicationError("TASK_LEASE_LOST", "任务执行租约已失效", 409)
        if access:
            await require_membership(session, ResourceScope(task.owner_user_id, task.workspace_id), write=True)
        if deadline and run.deadline_at is not None and run.deadline_at <= now:
            raise ApplicationError("DEADLINE_EXCEEDED", "任务超过执行截止时间", 409)
        return task, run

    def _event(
        self, session: AsyncSession, task: Task, run: TaskRun, name: str, data: dict[str, Any] | None = None
    ) -> None:
        """在持有任务行锁的事务内分配单调事件序号。

        Args:
            session: 当前事务。
            task: 已锁定任务。
            run: 当前执行。
            name: 安全事件类型。
            data: 可向任务所有者展示的数据。
        """
        session.add(
            TaskEvent(
                task_id=task.id,
                sequence=task.next_event_sequence,
                event_type=name,
                data={"run_id": str(run.id), **(data or {})},
            )
        )
        task.next_event_sequence += 1
        run.version += 1

    async def _snapshot(self, session: AsyncSession, task: Task) -> TaskSnapshot:
        """构建单事务权威快照，不读取模型的私有思维链。

        Args:
            session: 当前事务。
            task: 当前任务。

        Returns:
            任务、计划、步骤与事件游标。
        """
        run = await self._run(session, task)
        plan = await session.scalar(
            select(TaskPlan).where(TaskPlan.run_id == run.id).order_by(TaskPlan.revision.desc())
        )
        steps = await session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position))
        actions = list(
            await session.scalars(select(TaskAction).where(TaskAction.run_id == run.id).order_by(TaskAction.id))
        )
        action_ids = [action.id for action in actions]
        errors: dict[UUID, str | None] = {}
        if action_ids:
            attempts = await session.scalars(
                select(TaskActionAttempt)
                .where(TaskActionAttempt.action_id.in_(action_ids))
                .order_by(TaskActionAttempt.attempt_no.desc())
            )
            for attempt in attempts:
                errors.setdefault(attempt.action_id, attempt.error_code)
        return TaskSnapshot(
            task=TaskView.model_validate(task),
            run=TaskRunView.model_validate(run),
            plan=PlanView(revision=plan.revision, digest=plan.digest, **plan.definition) if plan else None,
            steps=[
                TaskStepView.model_validate(
                    {
                        **step.definition,
                        "status": step.status,
                        "attempt_count": step.attempt_count,
                        "result": step.result,
                        "error_code": step.error_code,
                        "next_retry_at": step.next_retry_at,
                    }
                )
                for step in steps
            ],
            actions=[
                TaskActionSummary(
                    id=action.id,
                    step_key=action.step_key,
                    kind=action.kind,
                    status=action.status,
                    attempt_count=action.attempt_count,
                    max_attempts=self.max_attempts,
                    error_code=errors.get(action.id),
                    next_retry_at=action.next_retry_at,
                )
                for action in actions
            ],
            last_event_sequence=task.next_event_sequence - 1,
        )

    async def create(
        self, scope: ResourceScope, goal: str, idempotency_key: str, source_conversation_id: UUID | None = None
    ) -> TaskSnapshot:
        """显式且幂等创建逻辑任务，不覆盖同键不同目标。

        Args:
            scope: 可信用户与空间。
            goal: 用户目标。
            idempotency_key: 用户提交的幂等键。
            source_conversation_id: 可选本人对话。

        Returns:
            新任务或同一逻辑任务快照。
        """
        if not goal.strip():
            raise ApplicationError("INVALID_TASK_GOAL", "任务目标不能为空", 422)
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CREATE_LOCK})
            existing = await session.scalar(
                select(Task)
                .where(
                    Task.workspace_id == scope.workspace_id,
                    Task.owner_user_id == scope.user_id,
                    Task.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.goal != goal or existing.source_conversation_id != source_conversation_id:
                    raise ApplicationError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同任务", 409)
                return await self._snapshot(session, existing)
            if source_conversation_id is not None:
                conversation = await session.scalar(
                    select(Conversation.id).where(
                        Conversation.id == source_conversation_id,
                        Conversation.workspace_id == scope.workspace_id,
                        Conversation.created_by_user_id == scope.user_id,
                    )
                )
                if conversation is None:
                    raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)
            task = Task(
                id=uuid4(),
                workspace_id=scope.workspace_id,
                owner_user_id=scope.user_id,
                goal=goal,
                idempotency_key=idempotency_key,
                source_conversation_id=source_conversation_id,
            )
            session.add(task)
            await session.flush()
            run = TaskRun(
                id=uuid4(), task_id=task.id, deadline_at=datetime.now(UTC) + timedelta(seconds=PLANNING_SECONDS)
            )
            session.add(run)
            await session.flush()
            task.current_run_id = run.id
            self._event(session, task, run, "task_created")
            await session.flush()
            return await self._snapshot(session, task)

    async def list(self, scope: ResourceScope, offset: int = 0, limit: int = 20) -> ListType[TaskSnapshot]:
        """列出本人当前空间任务。

        Args:
            scope: 当前身份。
            offset: 分页偏移。
            limit: 页大小。

        Returns:
            稳定排序的任务快照。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope)
            tasks = await session.scalars(
                select(Task)
                .where(Task.workspace_id == scope.workspace_id, Task.owner_user_id == scope.user_id)
                .order_by(Task.created_at.desc(), Task.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [await self._snapshot(session, task) for task in tasks]

    async def get(self, scope: ResourceScope, task_id: UUID) -> TaskSnapshot:
        """读取本人资源，管理员也没有他人内容旁路。

        Args:
            scope: 当前身份。
            task_id: 任务标识。

        Returns:
            权威快照。
        """
        async with self.factory.begin() as session:
            return await self._snapshot(session, await self._task(session, task_id, scope))

    async def system_get(self, task_id: UUID) -> TaskSnapshot:
        """后台调度读取任务并重验成员权限。

        Args:
            task_id: 后台已领取的任务。

        Returns:
            当前快照。
        """
        async with self.factory.begin() as session:
            task = await self._task(session, task_id)
            await require_membership(session, ResourceScope(task.owner_user_id, task.workspace_id), write=True)
            return await self._snapshot(session, task)

    async def claim(self, owner: str) -> TaskSnapshot | None:
        """领取到期且无有效租约的任务，恢复步骤但复用成功动作。

        Args:
            owner: 执行器实例标识。

        Returns:
            获得新 token 的任务或空值。
        """
        async with self.factory.begin() as session:
            now = datetime.now(UTC)
            task = await session.scalar(
                select(Task)
                .join(TaskRun, TaskRun.id == Task.current_run_id)
                .where(
                    TaskRun.status.in_(ACTIVE),
                    or_(TaskRun.lease_expires_at.is_(None), TaskRun.lease_expires_at <= now),
                    or_(TaskRun.status != "retry_wait", TaskRun.next_retry_at.is_(None), TaskRun.next_retry_at <= now),
                )
                .order_by(Task.created_at, Task.id)
                .with_for_update(of=Task, skip_locked=True)
                .limit(1)
            )
            if task is None:
                return None
            run = await self._run(session, task)
            try:
                await require_membership(session, ResourceScope(task.owner_user_id, task.workspace_id), write=True)
            except ApplicationError:
                run.status, run.error_code, run.finished_at = "failed", "ACCESS_REVOKED", now
                self._event(session, task, run, "task_failed", {"error_code": "ACCESS_REVOKED"})
                return None
            if run.deadline_at is not None and run.deadline_at <= now:
                run.status, run.error_code, run.finished_at = "failed", "DEADLINE_EXCEEDED", now
                self._event(session, task, run, "task_failed", {"error_code": "DEADLINE_EXCEEDED"})
                return None
            run.fencing_token += 1
            run.lease_owner, run.lease_expires_at = owner, now + timedelta(seconds=LEASE_SECONDS)
            if run.status == "pending":
                run.status = "planning"
            await session.execute(
                update(TaskStep).where(TaskStep.run_id == run.id, TaskStep.status == "running").values(status="pending")
            )
            step_ids = select(TaskStep.id).where(TaskStep.run_id == run.id)
            await session.execute(
                update(TaskStepAttempt)
                .where(TaskStepAttempt.step_id.in_(step_ids), TaskStepAttempt.status == "running")
                .values(status="interrupted", error_code="PROCESS_RESTARTED", finished_at=now)
            )
            action_ids = select(TaskAction.id).where(TaskAction.run_id == run.id)
            await session.execute(
                update(TaskActionAttempt)
                .where(TaskActionAttempt.action_id.in_(action_ids), TaskActionAttempt.status == "running")
                .values(status="interrupted", error_code="PROCESS_RESTARTED", finished_at=now)
            )
            self._event(session, task, run, "task_claimed", {"fencing_token": run.fencing_token})
            await session.flush()
            return await self._snapshot(session, task)

    async def heartbeat(self, task_id: UUID, run_id: UUID, token: int, owner: str) -> bool:
        """续租不改变业务版本，失效或撤权时不延长执行权。

        Args:
            task_id: 任务标识。
            run_id: 当前执行。
            token: 领取 token。
            owner: 执行器实例。

        Returns:
            是否成功续租。
        """
        async with self.factory.begin() as session:
            _, run = await self._guard(session, task_id, run_id, token)
            if run.lease_owner != owner:
                return False
            run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
            return True

    async def release(self, task_id: UUID, run_id: UUID, token: int, owner: str) -> None:
        """退出时只释放本执行器的租约，让下一进程立即恢复。

        Args:
            task_id: 任务标识。
            run_id: 当前执行。
            token: 领取 token。
            owner: 执行器标识。
        """
        async with self.factory.begin() as session:
            task = await self._task(session, task_id)
            run = await self._run(session, task)
            if run.id == run_id and run.fencing_token == token and run.lease_owner == owner:
                run.lease_owner, run.lease_expires_at = None, None

    async def save_plan(self, task_id: UUID, run_id: UUID, token: int, plan: PlanDraft) -> None:
        """保存已验证不可变计划，并暂停等待用户确认。

        Args:
            task_id: 任务标识。
            run_id: 当前执行。
            token: 当前租约。
            plan: 待保存的有限 DAG。
        """
        plan = PlanDraft.model_validate(plan.model_dump())
        encoded = json.dumps(plan.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            if run.confirmed_plan_revision is not None:
                raise ApplicationError("PLAN_ALREADY_CONFIRMED", "执行中的计划不能被原地覆盖", 409)
            previous = await session.scalar(select(func.max(TaskPlan.revision)).where(TaskPlan.run_id == run.id))
            revision = (previous or 0) + 1
            if revision > 3:
                raise ApplicationError("PLAN_REVISION_LIMIT", "计划修订次数已达上限", 409)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            session.add(
                TaskPlan(
                    task_id=task.id,
                    run_id=run.id,
                    revision=revision,
                    digest=digest,
                    definition=plan.model_dump(mode="json"),
                )
            )
            run.status, run.deadline_at, run.next_retry_at = "awaiting_confirmation", None, None
            run.lease_owner, run.lease_expires_at = None, None
            self._event(session, task, run, "plan_created", {"revision": revision, "digest": digest})

    async def confirm(
        self, scope: ResourceScope, task_id: UUID, revision: int, digest: str, expected_run_id: UUID | None = None
    ) -> TaskSnapshot:
        """确认精确版本，创建本轮步骤并启动截止时钟。

        Args:
            scope: 确认人身份。
            task_id: 任务标识。
            revision: 计划版本。
            digest: 完整计划摘要。
            expected_run_id: 客户端确认时所见的执行标识。

        Returns:
            可执行任务快照。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            task = await self._task(session, task_id, scope)
            run = await self._run(session, task)
            if expected_run_id is not None and run.id != expected_run_id:
                raise ApplicationError("TASK_VERSION_CONFLICT", "任务执行已变化，请刷新后重试", 409)
            plan = await session.scalar(
                select(TaskPlan).where(TaskPlan.run_id == run.id).order_by(TaskPlan.revision.desc())
            )
            if plan is None or plan.revision != revision or plan.digest != digest:
                raise ApplicationError("PLAN_VERSION_CONFLICT", "计划已变化，请重新查看并确认", 409)
            if run.confirmed_plan_revision == revision:
                return await self._snapshot(session, task)
            if run.status != "awaiting_confirmation":
                raise ApplicationError("TASK_STATE_CONFLICT", "当前任务不能确认计划", 409)
            draft = PlanDraft.model_validate(plan.definition)
            for position, step in enumerate(draft.steps):
                session.add(
                    TaskStep(
                        task_id=task.id,
                        run_id=run.id,
                        key=step.key,
                        position=position,
                        definition=step.model_dump(mode="json"),
                    )
                )
            run.confirmed_plan_revision, run.status = revision, "running"
            run.deadline_at = datetime.now(UTC) + timedelta(seconds=EXECUTION_SECONDS)
            self._event(session, task, run, "plan_confirmed", {"revision": revision})
            await session.flush()
            return await self._snapshot(session, task)

    async def control(
        self,
        scope: ResourceScope,
        task_id: UUID,
        action: str,
        expected_run_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> TaskSnapshot:
        """幂等控制任务；失败后重试新建执行，不篡改旧记录。

        Args:
            scope: 当前用户。
            task_id: 任务标识。
            action: pause、resume、cancel 或 retry。
            expected_run_id: 客户端所见的执行。
            expected_version: 客户端所见的状态版本。

        Returns:
            控制后的权威快照。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=action != "cancel")
            task = await self._task(session, task_id, scope)
            run = await self._run(session, task)
            now = datetime.now(UTC)
            if expected_run_id is not None and run.id != expected_run_id:
                raise ApplicationError("TASK_VERSION_CONFLICT", "任务执行已变化，请刷新后重试", 409)
            already_applied = (
                (action == "cancel" and run.status in TERMINAL)
                or (action == "pause" and run.status == "paused")
                or (action == "resume" and run.status in ACTIVE)
            )
            if already_applied:
                return await self._snapshot(session, task)
            if expected_version is not None and run.version != expected_version:
                raise ApplicationError("TASK_VERSION_CONFLICT", "任务状态已变化，请刷新后重试", 409)
            if action == "retry":
                if run.status not in ("failed", "cancelled"):
                    if run.status == "pending":
                        return await self._snapshot(session, task)
                    raise ApplicationError("TASK_STATE_CONFLICT", "只有失败或取消的执行可以重试", 409)
                run = TaskRun(id=uuid4(), task_id=task.id, deadline_at=now + timedelta(seconds=PLANNING_SECONDS))
                session.add(run)
                await session.flush()
                task.current_run_id = run.id
                self._event(session, task, run, "task_retried", {"reuse_completed_steps": False})
            elif action == "cancel":
                if run.status in TERMINAL:
                    return await self._snapshot(session, task)
                run.status, run.finished_at = "cancelled", now
                self._event(session, task, run, "task_cancelled")
            elif action == "pause":
                if run.status == "paused":
                    return await self._snapshot(session, task)
                if run.status not in ACTIVE:
                    raise ApplicationError("TASK_STATE_CONFLICT", "当前任务不能暂停", 409)
                run.paused_from, run.status = run.status, "paused"
                self._event(session, task, run, "task_paused")
            elif action == "resume":
                if run.status in ACTIVE:
                    return await self._snapshot(session, task)
                if run.status != "paused":
                    raise ApplicationError("TASK_STATE_CONFLICT", "当前任务不能恢复", 409)
                if run.deadline_at is not None and run.deadline_at <= now:
                    run.status, run.error_code, run.finished_at = "failed", "DEADLINE_EXCEEDED", now
                    self._event(session, task, run, "task_failed", {"error_code": "DEADLINE_EXCEEDED"})
                else:
                    run.status = run.paused_from or ("running" if run.confirmed_plan_revision else "planning")
                    self._event(session, task, run, "task_resumed")
            else:
                raise ApplicationError("INVALID_TASK_CONTROL", "不支持的任务操作", 422)
            if action != "retry":
                run.fencing_token += 1
                run.lease_owner, run.lease_expires_at = None, None
                if action in ("cancel", "pause"):
                    action_ids = select(TaskAction.id).where(TaskAction.run_id == run.id)
                    await session.execute(
                        update(TaskActionAttempt)
                        .where(TaskActionAttempt.action_id.in_(action_ids), TaskActionAttempt.status == "running")
                        .values(status="interrupted", finished_at=now, error_code="TASK_INTERRUPTED")
                    )
                    await session.execute(
                        update(TaskAction)
                        .where(TaskAction.run_id == run.id, TaskAction.status.in_(("running", "pending", "retry_wait")))
                        .values(status="cancelled" if action == "cancel" else "interrupted")
                    )
                    step_ids = select(TaskStep.id).where(TaskStep.run_id == run.id)
                    await session.execute(
                        update(TaskStepAttempt)
                        .where(TaskStepAttempt.step_id.in_(step_ids), TaskStepAttempt.status == "running")
                        .values(status="interrupted", finished_at=now, error_code="TASK_INTERRUPTED")
                    )
                    await session.execute(
                        update(TaskStep)
                        .where(TaskStep.run_id == run.id, TaskStep.status != "succeeded")
                        .values(status="cancelled" if action == "cancel" else "pending")
                    )
            await session.flush()
            return await self._snapshot(session, task)

    async def _step(self, session: AsyncSession, run_id: UUID, key: str) -> TaskStep:
        """查找当前运行的步骤。

        Args:
            session: 当前事务。
            run_id: 当前运行。
            key: 步骤标识。

        Returns:
            步骤记录。
        """
        step = await session.scalar(select(TaskStep).where(TaskStep.run_id == run_id, TaskStep.key == key))
        if step is None:
            raise ApplicationError("TASK_STEP_NOT_FOUND", "任务步骤不存在", 404)
        return step

    async def start_step(self, task_id: UUID, run_id: UUID, token: int, step_key: str) -> UUID:
        """依赖全部成功才开始步骤，重复开始不会重复计数。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            step_key: 步骤标识。

        Returns:
            本轮步骤尝试标识。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            step = await self._step(session, run.id, step_key)
            if step.status == "running":
                attempt = await session.scalar(
                    select(TaskStepAttempt.id)
                    .where(TaskStepAttempt.step_id == step.id, TaskStepAttempt.status == "running")
                    .order_by(TaskStepAttempt.attempt_no.desc())
                )
                if attempt is not None:
                    return attempt
            if step.status not in ("pending", "retry_wait"):
                raise ApplicationError("TASK_STEP_STATE_CONFLICT", "当前步骤不能执行", 409)
            completed = set(
                await session.scalars(
                    select(TaskStep.key).where(TaskStep.run_id == run.id, TaskStep.status == "succeeded")
                )
            )
            if not set(step.definition["depends_on"]) <= completed:
                raise ApplicationError("TASK_DEPENDENCY_NOT_READY", "上游步骤尚未完成", 409)
            if step.next_retry_at is not None and step.next_retry_at > datetime.now(UTC):
                raise ApplicationError("TASK_RETRY_NOT_DUE", "尚未到重试时间", 409)
            step.attempt_count += 1
            step.status, step.error_code, step.next_retry_at = "running", None, None
            new_attempt = TaskStepAttempt(id=uuid4(), step_id=step.id, attempt_no=step.attempt_count)
            session.add(new_attempt)
            run.status = "running"
            self._event(session, task, run, "step_started", {"step_key": step_key, "attempt": step.attempt_count})
            return new_attempt.id

    async def complete_step(self, task_id: UUID, run_id: UUID, token: int, step_key: str, result: str) -> None:
        """原子保存步骤结果、尝试终态和事件。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            step_key: 步骤。
            result: 已校验结果。
        """
        if not result.strip():
            raise ApplicationError("EMPTY_STEP_RESULT", "步骤未返回有效结果", 502)
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            step = await self._step(session, run.id, step_key)
            if step.status == "succeeded":
                return
            if step.status not in ("running", "retry_wait"):
                raise ApplicationError("TASK_STEP_STATE_CONFLICT", "当前步骤不能提交结果", 409)
            step.status, step.result, step.error_code, step.next_retry_at = "succeeded", result, None, None
            await session.execute(
                update(TaskStepAttempt)
                .where(TaskStepAttempt.step_id == step.id, TaskStepAttempt.status == "running")
                .values(status="completed", finished_at=datetime.now(UTC))
            )
            run.status = "running"
            run.next_retry_at = await session.scalar(
                select(func.min(TaskAction.next_retry_at)).where(
                    TaskAction.run_id == run.id, TaskAction.status == "retry_wait"
                )
            )
            self._event(session, task, run, "step_succeeded", {"step_key": step_key})

    async def fail(self, task_id: UUID, run_id: UUID, token: int, code: str) -> None:
        """撤权或异常时允许持有 token 的执行器安全收敛失败。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            code: 安全错误码。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token, access=False, deadline=False)
            now = datetime.now(UTC)
            run.status, run.error_code, run.finished_at = "failed", code, now
            run.lease_owner, run.lease_expires_at = None, None
            await session.execute(
                update(TaskStep)
                .where(TaskStep.run_id == run.id, TaskStep.status.in_(("running", "retry_wait")))
                .values(status="failed", error_code=code)
            )
            step_ids = select(TaskStep.id).where(TaskStep.run_id == run.id)
            await session.execute(
                update(TaskStepAttempt)
                .where(TaskStepAttempt.step_id.in_(step_ids), TaskStepAttempt.status == "running")
                .values(status="failed", error_code=code, finished_at=now)
            )
            action_ids = select(TaskAction.id).where(TaskAction.run_id == run.id)
            await session.execute(
                update(TaskActionAttempt)
                .where(TaskActionAttempt.action_id.in_(action_ids), TaskActionAttempt.status == "running")
                .values(status="failed", error_code=code, finished_at=now)
            )
            await session.execute(
                update(TaskAction)
                .where(TaskAction.run_id == run.id, TaskAction.status == "running")
                .values(status="failed", next_retry_at=None)
            )
            run.next_retry_at = None
            self._event(session, task, run, "task_failed", {"error_code": code})

    async def finish(self, task_id: UUID, run_id: UUID, token: int, result: str) -> None:
        """所有必要步骤成功后才能提交任务完成。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            result: 最终汇总。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            unfinished = await session.scalar(
                select(func.count())
                .select_from(TaskStep)
                .where(TaskStep.run_id == run.id, TaskStep.status != "succeeded")
            )
            count = await session.scalar(select(func.count()).select_from(TaskStep).where(TaskStep.run_id == run.id))
            if unfinished or not count or not result.strip():
                raise ApplicationError("TASK_INCOMPLETE", "任务步骤尚未全部完成", 409)
            run.status, run.result, run.finished_at, run.next_retry_at = "completed", result, datetime.now(UTC), None
            run.lease_owner, run.lease_expires_at = None, None
            self._event(session, task, run, "task_completed")

    async def start_action(
        self, task_id: UUID, run_id: UUID, token: int, step_key: str | None, kind: str, input_data: dict[str, Any]
    ) -> ActionView:
        """逻辑调用按步骤、种类和规范化输入幂等，成功结果可恢复。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            step_key: 规划阶段为空。
            kind: 模型或工具类型。
            input_data: 受保护执行输入，不含凭证。

        Returns:
            同一逻辑调用账本。
        """
        encoded = json.dumps(
            {"step": step_key, "kind": kind, "input": input_data},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            action = await session.scalar(
                select(TaskAction).where(TaskAction.run_id == run.id, TaskAction.input_digest == digest)
            )
            if action is None:
                if kind == "tool":
                    count = await session.scalar(
                        select(func.count())
                        .select_from(TaskAction)
                        .where(TaskAction.run_id == run.id, TaskAction.kind == "tool")
                    )
                    if (count or 0) >= 20:
                        raise ApplicationError("TASK_ACTION_LIMIT", "本次任务的工具调用次数已达上限", 409)
                action = TaskAction(
                    id=uuid4(),
                    task_id=task.id,
                    run_id=run.id,
                    step_key=step_key,
                    kind=kind,
                    input_digest=digest,
                    input_data=input_data,
                )
                session.add(action)
                await session.flush()
            view = ActionView.model_validate(action)
            view.error_code = await session.scalar(
                select(TaskActionAttempt.error_code)
                .where(TaskActionAttempt.action_id == action.id)
                .order_by(TaskActionAttempt.attempt_no.desc())
                .limit(1)
            )
            return view

    async def action_attempt(
        self, task_id: UUID, run_id: UUID, token: int, action_id: UUID, max_attempts: int = 3
    ) -> UUID:
        """每次真实请求计数；成功动作禁止再次外部调用。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            action_id: 逻辑请求标识。
            max_attempts: 包含首次在内的最大真实请求尝试数。

        Returns:
            本次请求尝试标识。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            action = await session.scalar(
                select(TaskAction).where(TaskAction.id == action_id, TaskAction.run_id == run.id)
            )
            if action is None or action.status == "succeeded":
                raise ApplicationError("TASK_ACTION_STATE_CONFLICT", "请求不存在或已完成", 409)
            if action.next_retry_at is not None and action.next_retry_at > datetime.now(UTC):
                raise ApplicationError("TASK_RETRY_NOT_DUE", "尚未到重试时间", 409)
            if action.kind in ("plan", "model"):
                model_actions = select(TaskAction.id).where(
                    TaskAction.run_id == run.id, TaskAction.kind.in_(("plan", "model"))
                )
                count = await session.scalar(
                    select(func.count())
                    .select_from(TaskActionAttempt)
                    .where(TaskActionAttempt.action_id.in_(model_actions))
                )
                if (count or 0) >= 30:
                    raise ApplicationError("TASK_ACTION_LIMIT", "本次任务的模型请求次数已达上限", 409)
            if action.attempt_count >= max_attempts:
                raise ApplicationError("TASK_RETRY_EXHAUSTED", "该请求的尝试次数已达上限", 409)
            action.attempt_count += 1
            action.status, action.next_retry_at = "running", None
            attempt = TaskActionAttempt(id=uuid4(), action_id=action.id, attempt_no=action.attempt_count)
            session.add(attempt)
            run.status = "running" if run.confirmed_plan_revision else "planning"
            if action.step_key is not None:
                step = await self._step(session, run.id, action.step_key)
                step.status, step.next_retry_at, step.error_code = "running", None, None
            self._event(
                session,
                task,
                run,
                "action_attempt_started",
                {
                    "step_key": action.step_key,
                    "action_id": str(action.id),
                    "attempt": action.attempt_count,
                    "generation_id": str(attempt.id),
                },
            )
            return attempt.id

    async def finish_action(
        self,
        task_id: UUID,
        run_id: UUID,
        token: int,
        action_id: UUID,
        attempt_id: UUID,
        result: str | None,
        code: str | None,
        retry_at: datetime | None = None,
        pause: bool = False,
    ) -> None:
        """尝试结果与检查点同事务提交，未提交结果不能被视为成功。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            action_id: 逻辑动作。
            attempt_id: 实际请求尝试。
            result: 完整结果，不保存未校验的半份工具参数。
            code: 安全失败码。
            retry_at: 可重试错误的最早重发时间，和错误状态同事务保存。
            pause: 是否因长等待暂停任务，不自动发起后续请求。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            action = await session.scalar(
                select(TaskAction).where(TaskAction.id == action_id, TaskAction.run_id == run.id)
            )
            attempt = await session.scalar(
                select(TaskActionAttempt).where(
                    TaskActionAttempt.id == attempt_id, TaskActionAttempt.action_id == action_id
                )
            )
            if action is None or attempt is None or attempt.status != "running":
                raise ApplicationError("TASK_ACTION_STATE_CONFLICT", "请求尝试状态已变化", 409)
            attempt.status, attempt.error_code, attempt.finished_at = (
                "failed" if code else "completed",
                code,
                datetime.now(UTC),
            )
            action.status, action.result = "failed" if code else "succeeded", None if code else result
            action.next_retry_at = None
            if retry_at is not None and code is not None:
                action.status, action.next_retry_at = "retry_wait", retry_at
                if action.step_key is not None:
                    step = await self._step(session, run.id, action.step_key)
                    step.status, step.next_retry_at, step.error_code = "retry_wait", retry_at, code
                await session.flush()
                active = await session.scalar(
                    select(func.count())
                    .select_from(TaskStep)
                    .where(TaskStep.run_id == run.id, TaskStep.status == "running")
                )
                run.status = "running" if active else "retry_wait"
                run.next_retry_at = await session.scalar(
                    select(func.min(TaskAction.next_retry_at)).where(
                        TaskAction.run_id == run.id, TaskAction.status == "retry_wait"
                    )
                )
                if pause:
                    run.status, run.paused_from, run.error_code = "paused", "retry_wait", code
                    run.fencing_token += 1
                    run.lease_owner, run.lease_expires_at = None, None
                self._event(
                    session,
                    task,
                    run,
                    "task_paused" if pause else "action_retry_scheduled",
                    {
                        "step_key": action.step_key,
                        "action_id": str(action.id),
                        "attempt": action.attempt_count,
                        "error_code": code,
                        "next_retry_at": retry_at.isoformat(),
                    },
                )
            else:
                self._event(
                    session,
                    task,
                    run,
                    "action_failed" if code else "action_succeeded",
                    {
                        "step_key": action.step_key,
                        "action_id": str(action.id),
                        "attempt": action.attempt_count,
                        "error_code": code,
                    },
                )

    async def retry_wait(
        self,
        task_id: UUID,
        run_id: UUID,
        token: int,
        step_key: str | None,
        action_id: UUID,
        next_retry_at: datetime,
        code: str,
    ) -> None:
        """持久化等待时间，不持有数据库连接休眠。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            step_key: 关联步骤或规划。
            action_id: 逻辑请求。
            next_retry_at: 不得早于供应商要求的实际时间。
            code: 失败类别。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            action = await session.scalar(
                select(TaskAction).where(TaskAction.id == action_id, TaskAction.run_id == run.id)
            )
            if action is None or action.status == "succeeded":
                raise ApplicationError("TASK_ACTION_STATE_CONFLICT", "请求不能进入等待", 409)
            if run.deadline_at is not None and next_retry_at >= run.deadline_at:
                raise ApplicationError("DEADLINE_EXCEEDED", "等待时间超过任务截止时间", 409)
            action.status, action.next_retry_at = "retry_wait", next_retry_at
            if step_key is not None:
                step = await self._step(session, run.id, step_key)
                step.status, step.next_retry_at, step.error_code = "retry_wait", next_retry_at, code
            await session.flush()
            active_steps = await session.scalar(
                select(func.count())
                .select_from(TaskStep)
                .where(TaskStep.run_id == run.id, TaskStep.status == "running")
            )
            run.status = "running" if active_steps else "retry_wait"
            run.next_retry_at = await session.scalar(
                select(func.min(TaskAction.next_retry_at)).where(
                    TaskAction.run_id == run.id, TaskAction.status == "retry_wait"
                )
            )
            self._event(
                session,
                task,
                run,
                "action_retry_scheduled",
                {
                    "step_key": step_key,
                    "action_id": str(action_id),
                    "attempt": action.attempt_count,
                    "error_code": code,
                    "next_retry_at": next_retry_at.isoformat(),
                },
            )

    async def events(
        self, scope: ResourceScope, task_id: UUID, after: int = 0, limit: int = 100
    ) -> ListType[TaskEventView]:
        """有序事件读取必须与详情共享归属边界。

        Args:
            scope: 当前身份。
            task_id: 任务标识。
            after: 已消费游标。
            limit: 页大小。

        Returns:
            可安全重放的事件。
        """
        async with self.factory.begin() as session:
            await self._task(session, task_id, scope)
            rows = await session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id, TaskEvent.sequence > after)
                .order_by(TaskEvent.sequence)
                .limit(limit)
            )
            return [TaskEventView.model_validate(row) for row in rows]

    async def pause_wait(self, task_id: UUID, run_id: UUID, token: int, next_retry_at: datetime, code: str) -> None:
        """服务端要求长等待时持久化暂停，恢复不能绕过最早重试时间。

        Args:
            task_id: 任务标识。
            run_id: 执行标识。
            token: 当前租约。
            next_retry_at: 供应商要求的最早重试时间。
            code: 安全暂停原因。
        """
        async with self.factory.begin() as session:
            task, run = await self._guard(session, task_id, run_id, token)
            if run.deadline_at is not None and next_retry_at >= run.deadline_at:
                raise ApplicationError("DEADLINE_EXCEEDED", "等待时间超过任务截止时间", 409)
            run.status, run.paused_from, run.next_retry_at, run.error_code = "paused", "retry_wait", next_retry_at, code
            run.fencing_token += 1
            run.lease_owner, run.lease_expires_at = None, None
            await session.execute(
                update(TaskStep)
                .where(TaskStep.run_id == run.id, TaskStep.status == "running")
                .values(status="pending", next_retry_at=next_retry_at)
            )
            self._event(
                session, task, run, "task_paused", {"error_code": code, "next_retry_at": next_retry_at.isoformat()}
            )

    async def check(self, task_id: UUID, run_id: UUID, token: int) -> None:
        """没有供应商输出时也复核取消、权限、租约和截止时间。

        Args:
            task_id: 当前任务。
            run_id: 当前执行。
            token: 持有的执行权。
        """
        async with self.factory.begin() as session:
            await self._guard(session, task_id, run_id, token)

    async def revise(
        self,
        scope: ResourceScope,
        task_id: UUID,
        plan: PlanDraft,
        expected_run_id: UUID | None = None,
        expected_revision: int | None = None,
    ) -> TaskSnapshot:
        """仅在等待确认阶段追加版本，不覆盖已经确认的计划。

        Args:
            scope: 可信用户与空间。
            task_id: 任务。
            plan: 完整新草案。
            expected_run_id: 编辑时所见的执行标识。
            expected_revision: 编辑时所见的最新计划版本。

        Returns:
            待重新确认的计划快照。
        """
        plan = PlanDraft.model_validate(plan.model_dump())
        encoded = json.dumps(plan.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            task = await self._task(session, task_id, scope)
            run = await self._run(session, task)
            if run.status != "awaiting_confirmation":
                raise ApplicationError("TASK_STATE_CONFLICT", "只有待确认的草案可以修改", 409)
            previous = await session.scalar(select(func.max(TaskPlan.revision)).where(TaskPlan.run_id == run.id))
            if expected_run_id is not None and run.id != expected_run_id:
                raise ApplicationError("TASK_VERSION_CONFLICT", "任务执行已变化，请刷新后重试", 409)
            if expected_revision is not None and previous != expected_revision:
                raise ApplicationError("PLAN_VERSION_CONFLICT", "计划已变化，请刷新后再编辑", 409)
            revision = (previous or 0) + 1
            if revision > 3:
                raise ApplicationError("PLAN_REVISION_LIMIT", "计划修订次数已达上限", 409)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            session.add(
                TaskPlan(
                    task_id=task.id,
                    run_id=run.id,
                    revision=revision,
                    digest=digest,
                    definition=plan.model_dump(mode="json"),
                )
            )
            self._event(session, task, run, "plan_created", {"revision": revision, "digest": digest})
            await session.flush()
            return await self._snapshot(session, task)

    async def history(self, scope: ResourceScope, task_id: UUID) -> ListType[TaskRunView]:
        """保留手动重试之前的执行记录。

        Args:
            scope: 当前用户。
            task_id: 任务。

        Returns:
            历次执行，不包含他人资料。
        """
        async with self.factory.begin() as session:
            task = await self._task(session, task_id, scope)
            rows = await session.scalars(
                select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
            )
            return [TaskRunView.model_validate(row) for row in rows]
