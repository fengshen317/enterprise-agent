"""定时任务短事务；归属校验与到期领取均在数据库内完成。"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.models.agent import AgentProfile, WorkspaceToolPolicy
from app.models.approval import Approval, ApprovalEvent
from app.models.conversation import Conversation, Run
from app.models.schedule import ScheduledTask
from app.schemas.auth import ResourceScope
from app.schemas.schedule import CreateScheduledTaskArgs, ScheduleInput, ScheduleView, UpdateScheduledTaskArgs
from app.services.auth import owned_conversations, require_membership
from app.services.chat_store import ChatStore
from app.services.schedule_calendar import CALENDAR_YEARS, CHINA_TIMEZONE, calendar_allows
from app.services.scheduling import next_scheduled_run, timezone_info, validate_expression

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedSchedule:
    """已原子领取的一次触发，不包含可被客户端伪造的身份字段。"""

    schedule_id: UUID
    workspace_id: UUID
    owner_user_id: UUID
    conversation_id: UUID
    prompt: str
    scheduled_for: datetime


class ScheduleStore:
    """管理定时任务与专用对话，不在事务内调用模型或启动生成。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], default_timezone: str) -> None:
        """保存会话工厂与默认时区。

        Args:
            factory: 应用独立会话工厂。
            default_timezone: 客户端未提供时区时使用的 IANA 名称。
        """
        self.factory = factory
        self.default_timezone = default_timezone

    @staticmethod
    def _view(schedule: ScheduledTask, conversation_title: str) -> ScheduleView:
        """把 ORM 记录转换为附带对话标题的公开视图。

        Args:
            schedule: 已持久化记录。
            conversation_title: 固定对话标题。

        Returns:
            不包含归属字段的公开视图。
        """
        return ScheduleView(
            id=schedule.id,
            conversation_id=schedule.conversation_id,
            conversation_title=conversation_title,
            name=schedule.name,
            prompt=schedule.prompt,
            cron_expression=schedule.cron_expression,
            timezone=schedule.timezone,
            calendar_mode=schedule.calendar_mode,
            calendar_waiting_year=schedule.calendar_waiting_year,
            revision=schedule.revision,
            enabled=schedule.enabled,
            push_to_wechat=schedule.push_to_wechat,
            next_run_at=schedule.next_run_at,
            last_run_at=schedule.last_run_at,
            last_run_id=schedule.last_run_id,
            last_status=schedule.last_status,
            last_error_code=schedule.last_error_code,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
        )

    async def _conversation(self, session: AsyncSession, scope: ResourceScope, conversation_id: UUID) -> Conversation:
        """确认固定对话属于当前用户和空间且仍可追加消息。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            conversation_id: 目标对话。

        Returns:
            已校验的对话。

        Raises:
            ApplicationError: 对话不存在或已归档。
        """
        conversation = await session.scalar(owned_conversations(scope).where(Conversation.id == conversation_id))
        if conversation is None:
            raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)
        if conversation.status != "active":
            raise ApplicationError("CONVERSATION_ARCHIVED", "归档对话不能创建定时任务", 409)
        return conversation

    def _schedule_values(self, body: ScheduleInput) -> tuple[str, str]:
        """校验并解析表达式与时区。

        Args:
            body: 已通过结构校验的输入。

        Returns:
            规范化表达式和时区。
        """
        expression = validate_expression(body.cron_expression)
        timezone = body.timezone or self.default_timezone
        timezone_info(timezone)
        return expression, timezone

    @staticmethod
    async def _private_agent(
        session: AsyncSession, scope: ResourceScope, agent_id: UUID, *, lock: bool
    ) -> AgentProfile:
        """审批和执行使用相同的目标范围，避免通过预览读取他人的配置。

        Args:
            session: 已验证成员的事务。
            scope: 可信用户和空间。
            agent_id: 请求绑定的 Agent。
            lock: 创建任务时锁住配置直到提交，避免与归档操作交错。

        Returns:
            本人的可用私有 Agent。

        Raises:
            ApplicationError: Agent 不可访问或已归档。
        """
        query = select(AgentProfile).where(
            AgentProfile.id == agent_id,
            AgentProfile.workspace_id == scope.workspace_id,
            AgentProfile.owner_user_id == scope.user_id,
            AgentProfile.visibility == "private",
        )
        agent = await session.scalar(query.with_for_update(read=True) if lock else query)
        if agent is None:
            raise ApplicationError("AGENT_NOT_FOUND", "未找到本人的私有 Agent", 404)
        if agent.status != "active":
            raise ApplicationError("AGENT_UNAVAILABLE", "Agent 已归档，不能绑定定时任务", 409)
        return agent

    async def private_agent_name(self, scope: ResourceScope, agent_id: UUID) -> str:
        """为审批预览解析目标名称，不允许模型提供显示名称。

        Args:
            scope: 可信用户和空间。
            agent_id: 请求绑定的 Agent。

        Returns:
            可用于本次任务的私有 Agent 名称。
        """
        async with self.factory() as session:
            await require_membership(session, scope, write=True)
            agent = await self._private_agent(session, scope, agent_id, lock=False)
            return agent.name

    async def create(
        self, scope: ResourceScope, body: ScheduleInput, *, agent_profile_id: UUID | None = None
    ) -> ScheduleView:
        """在当前用户和空间下创建定时任务。

        Args:
            scope: 可信用户和空间。
            body: 定时任务输入。
            agent_profile_id: 需要新建专用对话时使用的本人私有 Agent。

        Returns:
            新建的定时任务。
        """
        async with self.factory.begin() as session:
            return await self._create(session, scope, body, agent_profile_id)

    async def _create(
        self,
        session: AsyncSession,
        scope: ResourceScope,
        body: ScheduleInput,
        agent_profile_id: UUID | None,
        schedule_id: UUID | None = None,
    ) -> ScheduleView:
        """复用调用方事务，让任务、专用对话和审批执行记录一起提交。

        Args:
            session: 调用方持有的事务。
            scope: 已确认的用户及空间。
            body: 定时任务配置和来源对话。
            agent_profile_id: 需要创建专用对话时绑定的助手。
            schedule_id: 已审批工具调用使用的固定主键；网页创建时自动分配。

        Returns:
            新任务及其对话标题。
        """
        await require_membership(session, scope, write=True)
        conversation = await self._conversation(session, scope, body.conversation_id)
        expression, timezone = self._schedule_values(body)
        next_run_at, waiting_year = (
            next_scheduled_run(expression, timezone, body.calendar_mode) if body.enabled else (None, None)
        )
        if agent_profile_id is not None:
            agent = await self._private_agent(session, scope, agent_profile_id, lock=True)
            conversation = await ChatStore.create_conversation_in_session(session, body.name, scope, agent.id)
        schedule = ScheduledTask(
            id=schedule_id,
            workspace_id=scope.workspace_id,
            owner_user_id=scope.user_id,
            conversation_id=conversation.id,
            name=body.name,
            prompt=body.prompt,
            cron_expression=expression,
            timezone=timezone,
            calendar_mode=body.calendar_mode,
            calendar_waiting_year=waiting_year,
            enabled=body.enabled,
            push_to_wechat=body.push_to_wechat,
            next_run_at=next_run_at,
        )
        session.add(schedule)
        await session.flush()
        return self._view(schedule, conversation.title)

    async def execute_approved(self, run_id: UUID, call_id: UUID, *, enabled: bool = True) -> ScheduleView:
        """以已批准的调用为唯一键，并在同一事务内复核权限、创建任务和记录执行。

        Args:
            run_id: 发起工具调用的运行。
            call_id: 服务端在审批前分配的工具调用标识。
            enabled: 恢复时服务和工具的全局开关。

        Returns:
            本次审批实际创建的定时任务。

        Raises:
            ApplicationError: 权限、运行状态或审批参数已失效。
        """
        async with self.factory.begin() as session:
            run = await session.get(Run, run_id, with_for_update=True)
            approval = await session.scalar(
                select(Approval)
                .where(
                    Approval.run_id == run_id,
                    Approval.tool_call_id == call_id,
                    Approval.tool_name == "create_scheduled_task",
                    Approval.tool_version == 1,
                )
                .with_for_update()
            )
            if run is None or approval is None or approval.decided_by_user_id != approval.requested_by_user_id:
                raise ApplicationError("APPROVAL_REQUIRED", "缺少有效的定时任务审批", 409)
            scope = ResourceScope(approval.requested_by_user_id, approval.workspace_id)
            await require_membership(session, scope, write=True)
            await self._conversation(session, scope, run.conversation_id)
            if approval.status == "executed":
                # 已删除的任务不会因重试复活。
                task = await self._owned_task(session, scope, call_id)
                title = await session.scalar(select(Conversation.title).where(Conversation.id == task.conversation_id))
                return self._view(task, title or "")
            if approval.status != "approved" or run.status != "running" or run.cancel_requested_at is not None:
                raise ApplicationError("APPROVAL_INACTIVE", "审批或运行已结束", 409)
            policy = await session.scalar(
                select(WorkspaceToolPolicy.enabled).filter_by(
                    workspace_id=scope.workspace_id,
                    tool_key=approval.tool_name,
                )
            )
            if not enabled or policy is False:
                raise ApplicationError("TOOL_NOT_ALLOWED", "当前空间已停用此工具", 403)
            payload = json.dumps(approval.execution_args, ensure_ascii=False, sort_keys=True)
            if hashlib.sha256(payload.encode()).hexdigest() != approval.args_digest:
                raise ApplicationError("APPROVAL_CHANGED", "审批参数与原始摘要不一致", 409)
            try:
                args = CreateScheduledTaskArgs.model_validate(approval.execution_args)
                body = ScheduleInput(
                    **args.model_dump(exclude={"agent_profile_id"}),
                    conversation_id=run.conversation_id,
                )
            except ValidationError as exc:
                raise ApplicationError("APPROVAL_CHANGED", "无法还原定时任务参数", 409) from exc
            view = await self._create(session, scope, body, args.agent_profile_id, call_id)
            now = datetime.now(UTC)
            approval.status, approval.executed_at, approval.updated_at = "executed", now, now
            session.add(ApprovalEvent(approval_id=approval.id, action="executed", actor_role="system"))
            return view

    async def list(self, scope: ResourceScope, offset: int, limit: int) -> list[ScheduleView]:
        """按创建时间倒序列出当前用户的定时任务。

        Args:
            scope: 可信用户和空间。
            offset: 跳过数量。
            limit: 页面大小。

        Returns:
            当前用户自己的定时任务。
        """
        async with self.factory() as session:
            await require_membership(session, scope)
            rows = list(
                await session.scalars(
                    select(ScheduledTask)
                    .where(
                        ScheduledTask.workspace_id == scope.workspace_id,
                        ScheduledTask.owner_user_id == scope.user_id,
                    )
                    .order_by(ScheduledTask.created_at.desc(), ScheduledTask.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            if not rows:
                return []
            titles: dict[UUID, str] = {}
            for conversation_id, title in (
                await session.execute(
                    select(Conversation.id, Conversation.title).where(
                        Conversation.id.in_({row.conversation_id for row in rows})
                    )
                )
            ).all():
                titles[conversation_id] = title
            return [self._view(row, titles.get(row.conversation_id, "")) for row in rows]

    async def get(self, scope: ResourceScope, schedule_id: UUID) -> ScheduleView:
        """读取本人当前空间的一条定时任务。

        Args:
            scope: 可信用户和空间。
            schedule_id: 定时任务标识。

        Returns:
            公开视图。

        Raises:
            ApplicationError: 不属于当前用户时统一返回不存在。
        """
        async with self.factory() as session:
            await require_membership(session, scope)
            row = await self._owned_task(session, scope, schedule_id)
            title = await session.scalar(select(Conversation.title).where(Conversation.id == row.conversation_id))
            return self._view(row, title or "")

    @staticmethod
    async def _owned_task(
        session: AsyncSession, scope: ResourceScope, schedule_id: UUID, *, lock: bool = False
    ) -> ScheduledTask:
        """按可信用户与空间查找任务；改配置时锁住同一行。"""
        query = select(ScheduledTask).where(
            ScheduledTask.id == schedule_id,
            ScheduledTask.workspace_id == scope.workspace_id,
            ScheduledTask.owner_user_id == scope.user_id,
        )
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise ApplicationError("SCHEDULE_NOT_FOUND", "定时任务不存在", 404)
        return row

    @staticmethod
    def check_revision(current: int, expected: int | None) -> None:
        """避免迟到的页面保存或批准覆盖已经变化的配置。"""
        if expected is not None and current != expected:
            raise ApplicationError("SCHEDULE_VERSION_CONFLICT", "定时任务配置已变化，请重新查看后再修改", 409)

    async def _apply_update(
        self, session: AsyncSession, scope: ResourceScope, row: ScheduledTask, body: ScheduleInput
    ) -> ScheduleView:
        """让页面编辑和对话编辑遵守同一校验和触发规则。"""
        self.check_revision(row.revision, body.revision)
        conversation = await self._conversation(session, scope, body.conversation_id)
        expression, timezone = self._schedule_values(body)
        if (expression, timezone, body.calendar_mode, body.enabled) != (
            row.cron_expression, row.timezone, row.calendar_mode, row.enabled
        ):
            row.next_run_at, row.calendar_waiting_year = (
                next_scheduled_run(expression, timezone, body.calendar_mode) if body.enabled else (None, None)
            )
        row.conversation_id = conversation.id
        row.name = body.name
        row.prompt = body.prompt
        row.cron_expression = expression
        row.timezone = timezone
        row.calendar_mode = body.calendar_mode
        row.enabled = body.enabled
        row.push_to_wechat = body.push_to_wechat
        row.revision += 1
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return self._view(row, conversation.title)

    async def update(self, scope: ResourceScope, schedule_id: UUID, body: ScheduleInput) -> ScheduleView:
        """完整更新定时任务，仅在调度规则变化时重算触发时间。

        Args:
            scope: 可信用户和空间。
            schedule_id: 定时任务标识。
            body: 完整可编辑字段。

        Returns:
            更新后的定时任务。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            row = await self._owned_task(session, scope, schedule_id, lock=True)
            if "calendar_mode" not in body.model_fields_set:
                body = body.model_copy(update={"calendar_mode": row.calendar_mode})
            return await self._apply_update(session, scope, row, body)

    async def patch(self, scope: ResourceScope, changes: UpdateScheduledTaskArgs) -> ScheduleView:
        """在审批版本仍有效时仅替换 Agent 指定的字段。"""
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            row = await self._owned_task(session, scope, changes.schedule_id, lock=True)
            data = self._view(row, "").model_dump(include=set(ScheduleInput.model_fields))
            data.update(changes.changes())
            data["revision"] = changes.revision
            return await self._apply_update(session, scope, row, ScheduleInput.model_validate(data))

    async def set_enabled(self, scope: ResourceScope, schedule_id: UUID, enabled: bool) -> ScheduleView:
        """只修改任务启停状态，供经过审批的对话工具调用。

        Args:
            scope: 可信用户和空间。
            schedule_id: 定时任务标识。
            enabled: 是否启用。

        Returns:
            更新后的定时任务。

        Raises:
            ApplicationError: 任务不存在或固定对话不可用。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            row = await self._owned_task(session, scope, schedule_id, lock=True)
            conversation = await self._conversation(session, scope, row.conversation_id)
            if row.enabled != enabled:
                row.enabled = enabled
                row.next_run_at, row.calendar_waiting_year = (
                    next_scheduled_run(row.cron_expression, row.timezone, row.calendar_mode)
                    if enabled else (None, None)
                )
                row.revision += 1
                row.updated_at = datetime.now(UTC)
                await session.flush()
            return self._view(row, conversation.title)

    async def set_wechat_push(self, scope: ResourceScope, schedule_id: UUID, enabled: bool) -> ScheduleView:
        """只修改微信推送开关，保持 Cron 状态和下一次触发时间不变。

        Args:
            scope: 可信用户和空间。
            schedule_id: 定时任务标识。
            enabled: 是否推送到微信。

        Returns:
            更新后的定时任务。

        Raises:
            ApplicationError: 任务不存在。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            row = await self._owned_task(session, scope, schedule_id, lock=True)
            title = await session.scalar(select(Conversation.title).where(Conversation.id == row.conversation_id))
            if row.push_to_wechat != enabled:
                row.push_to_wechat = enabled
                row.revision += 1
                row.updated_at = datetime.now(UTC)
                await session.flush()
            return self._view(row, title or "")

    async def delete(self, scope: ResourceScope, schedule_id: UUID) -> None:
        """删除本人当前空间的定时任务，不影响已产生的对话和运行。

        Args:
            scope: 可信用户和空间。
            schedule_id: 定时任务标识。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            found = await session.scalar(
                select(ScheduledTask.id)
                .where(
                    ScheduledTask.id == schedule_id,
                    ScheduledTask.workspace_id == scope.workspace_id,
                    ScheduledTask.owner_user_id == scope.user_id,
                )
                .with_for_update()
            )
            if found is None:
                raise ApplicationError("SCHEDULE_NOT_FOUND", "定时任务不存在", 404)
            await session.execute(delete(ScheduledTask).where(ScheduledTask.id == schedule_id))

    async def claim_due(self, now: datetime) -> ClaimedSchedule | None:
        """原子领取一条到期任务并立即推进下一次触发。

        Args:
            now: 当前 UTC 时间。

        Returns:
            到期任务快照；没有到期任务时返回空。

        Notes:
            `SKIP LOCKED` 避免多进程重复领取；先推进 `next_run_at` 再执行，
            因此进程在创建运行前崩溃时最多丢失一次触发，不会重复执行。
        """
        async with self.factory.begin() as session:
            while True:
                row = await session.scalar(
                    select(ScheduledTask)
                    .where(
                        ScheduledTask.enabled.is_(True),
                        or_(
                            ScheduledTask.next_run_at <= now,
                            *(
                                and_(
                                    ScheduledTask.calendar_mode == mode, ScheduledTask.calendar_waiting_year.in_(years)
                                )
                                for mode, years in CALENDAR_YEARS.items()
                            ),
                        ),
                    )
                    .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                scheduled_for = row.next_run_at
                try:
                    row.next_run_at, row.calendar_waiting_year = next_scheduled_run(
                        row.cron_expression, row.timezone, row.calendar_mode, now
                    )
                except ApplicationError:
                    # 只有手工改库才会出现非法表达式；停用而不是反复领取同一条坏数据。
                    row.enabled = False
                    row.next_run_at = None
                    row.calendar_waiting_year = None
                    row.revision += 1
                    row.updated_at = now
                    logger.warning("schedule_disabled_invalid_cron", extra={"schedule_id": str(row.id)})
                    continue
                row.updated_at = now
                # 重启后可能领取过去的时间；休市或休假当天不执行积压的自动触发。
                if scheduled_for is None or not all(
                    calendar_allows(row.calendar_mode, candidate.astimezone(CHINA_TIMEZONE).date())
                    for candidate in (scheduled_for, now)
                ):
                    continue
                return ClaimedSchedule(
                    schedule_id=row.id,
                    workspace_id=row.workspace_id,
                    owner_user_id=row.owner_user_id,
                    conversation_id=row.conversation_id,
                    prompt=row.prompt,
                    scheduled_for=scheduled_for,
                )

    async def record_started(self, schedule_id: UUID, run_id: UUID, at: datetime) -> None:
        """记录本次触发已创建真实运行。

        Args:
            schedule_id: 定时任务标识。
            run_id: 新建运行标识。
            at: 触发时间。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ScheduledTask)
                .where(ScheduledTask.id == schedule_id)
                .values(
                    last_run_at=at,
                    last_run_id=run_id,
                    last_status="started",
                    last_error_code=None,
                    updated_at=at,
                )
            )

    async def record_skipped(self, schedule_id: UUID, at: datetime, error_code: str) -> None:
        """记录本次触发被安全跳过，不伪造成功运行。

        Args:
            schedule_id: 定时任务标识。
            at: 触发时间。
            error_code: 稳定错误码。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ScheduledTask)
                .where(ScheduledTask.id == schedule_id)
                .values(
                    last_run_at=at,
                    last_run_id=None,
                    last_status="skipped",
                    last_error_code=error_code,
                    updated_at=at,
                )
            )
