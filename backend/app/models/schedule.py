"""定时问答任务；归属和固定对话均由服务端绑定。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.services.schedule_calendar import ScheduleCalendar


class ScheduledTask(Base):
    """按 Cron 规则向固定对话追加一次真实问答运行。"""

    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("length(trim(prompt)) > 0", name="prompt_nonempty"),
        CheckConstraint("length(trim(cron_expression)) > 0", name="cron_nonempty"),
        CheckConstraint("length(trim(timezone)) > 0", name="timezone_nonempty"),
        CheckConstraint(
            "NOT enabled OR next_run_at IS NOT NULL OR calendar_waiting_year IS NOT NULL", name="enabled_next_run"
        ),
        CheckConstraint("calendar_mode IN ('all_days', 'cn_workday', 'cn_a_share')", name="calendar_mode"),
        CheckConstraint(
            "calendar_waiting_year IS NULL OR (enabled AND next_run_at IS NULL AND calendar_mode != 'all_days')",
            name="calendar_waiting",
        ),
        CheckConstraint("last_status IS NULL OR last_status IN ('started', 'skipped')", name="last_status"),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_scheduled_tasks_membership",
        ),
        Index(
            "ix_scheduled_tasks_due",
            "next_run_at",
            postgresql_where=text("enabled"),
        ),
        Index("ix_scheduled_tasks_owner_created", "workspace_id", "owner_user_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column()
    owner_user_id: Mapped[UUID] = mapped_column()
    # 结果续接到固定对话；删除对话时同时移除依赖它的定时任务。
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str] = mapped_column(Text)
    cron_expression: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64))
    calendar_mode: Mapped[ScheduleCalendar] = mapped_column(String(16), default="all_days", server_default="all_days")
    calendar_waiting_year: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # 主动外发必须由用户逐任务开启，避免新增渠道后旧任务突然产生通知。
    push_to_wechat: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
