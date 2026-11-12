"""定时任务短事务；归属校验与到期领取均在数据库内完成。"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.models.conversation import Conversation
from app.models.schedule import ScheduledTask
from app.schemas.auth import ResourceScope
from app.schemas.schedule import ScheduleInput, ScheduleView
from app.services.auth import owned_conversations, require_membership
from app.services.scheduling import next_occurrence, timezone_info, validate_expression

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
    """只读写定时任务表，不在事务内调用模型或启动生成。"""

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
            enabled=schedule.enabled,
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

    def _schedule_values(self, body: ScheduleInput) -> tuple[str, str, datetime | None]:
        """校验表达式与时区并计算下一次触发。

        Args:
            body: 已通过结构校验的输入。

        Returns:
            规范化表达式、时区和可空的下次触发时间。
        """
        expression = validate_expression(body.cron_expression)
        timezone = body.timezone or self.default_timezone
        timezone_info(timezone)
        return expression, timezone, next_occurrence(expression, timezone) if body.enabled else None

    async def create(self, scope: ResourceScope, body: ScheduleInput) -> ScheduleView:
        """在当前用户和空间下创建定时任务。

        Args:
            scope: 可信用户和空间。
            body: 定时任务输入。

        Returns:
            新建的定时任务。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            conversation = await self._conversation(session, scope, body.conversation_id)
            expression, timezone, next_run_at = self._schedule_values(body)
            schedule = ScheduledTask(
                workspace_id=scope.workspace_id,
                owner_user_id=scope.user_id,
                conversation_id=conversation.id,
                name=body.name,
                prompt=body.prompt,
                cron_expression=expression,
                timezone=timezone,
                enabled=body.enabled,
                next_run_at=next_run_at,
            )
            session.add(schedule)
            await session.flush()
            return self._view(schedule, conversation.title)

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
            row = await session.scalar(
                select(ScheduledTask).where(
                    ScheduledTask.id == schedule_id,
                    ScheduledTask.workspace_id == scope.workspace_id,
                    ScheduledTask.owner_user_id == scope.user_id,
                )
            )
            if row is None:
                raise ApplicationError("SCHEDULE_NOT_FOUND", "定时任务不存在", 404)
            title = await session.scalar(select(Conversation.title).where(Conversation.id == row.conversation_id))
            return self._view(row, title or "")

    async def update(self, scope: ResourceScope, schedule_id: UUID, body: ScheduleInput) -> ScheduleView:
        """完整更新定时任务，并在启用时重新计算下一次触发。

        Args:
            scope: 可信用户和空间。
            schedule_id: 定时任务标识。
            body: 完整可编辑字段。

        Returns:
            更新后的定时任务。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            row = await session.scalar(
                select(ScheduledTask)
                .where(
                    ScheduledTask.id == schedule_id,
                    ScheduledTask.workspace_id == scope.workspace_id,
                    ScheduledTask.owner_user_id == scope.user_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ApplicationError("SCHEDULE_NOT_FOUND", "定时任务不存在", 404)
            conversation = await self._conversation(session, scope, body.conversation_id)
            expression, timezone, next_run_at = self._schedule_values(body)
            row.conversation_id = conversation.id
            row.name = body.name
            row.prompt = body.prompt
            row.cron_expression = expression
            row.timezone = timezone
            row.enabled = body.enabled
            row.next_run_at = next_run_at
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return self._view(row, conversation.title)

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
                        ScheduledTask.next_run_at.is_not(None),
                        ScheduledTask.next_run_at <= now,
                    )
                    .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                scheduled_for = row.next_run_at
                if scheduled_for is None:
                    return None
                try:
                    following = next_occurrence(row.cron_expression, row.timezone, now)
                except ApplicationError:
                    # 只有手工改库才会出现非法表达式；停用而不是反复领取同一条坏数据。
                    row.enabled = False
                    row.next_run_at = None
                    row.updated_at = now
                    logger.warning("schedule_disabled_invalid_cron", extra={"schedule_id": str(row.id)})
                    continue
                row.next_run_at = following
                row.updated_at = now
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
