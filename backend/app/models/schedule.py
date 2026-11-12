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
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScheduledTask(Base):
    """按 Cron 规则向固定对话追加一次真实问答运行。"""

    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("length(trim(prompt)) > 0", name="prompt_nonempty"),
        CheckConstraint("length(trim(cron_expression)) > 0", name="cron_nonempty"),
        CheckConstraint("length(trim(timezone)) > 0", name="timezone_nonempty"),
        CheckConstraint("NOT enabled OR next_run_at IS NOT NULL", name="enabled_next_run"),
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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
