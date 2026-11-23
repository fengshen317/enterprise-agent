"""工作空间内可查询的结构化诊断事件。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SystemLogEvent(Base):
    """保留诊断标识和脱敏详情，关联记录删除后仍可用于排查。"""

    __tablename__ = "system_log_events"
    __table_args__ = (
        Index("ix_system_log_workspace_time", "workspace_id", "occurred_at", "id"),
        Index("ix_system_log_workspace_request", "workspace_id", "request_id"),
        Index("ix_system_log_workspace_run", "workspace_id", "run_id"),
        Index("ix_system_log_workspace_tool", "workspace_id", "tool_call_id"),
        Index("ix_system_log_workspace_task", "workspace_id", "task_execution_id"),
        Index("ix_system_log_occurred_at", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(48))
    outcome: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column()
    request_id: Mapped[str | None] = mapped_column(String(32))
    run_id: Mapped[UUID | None] = mapped_column()
    tool_call_id: Mapped[UUID | None] = mapped_column()
    connector_key: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(128))
    task_execution_id: Mapped[UUID | None] = mapped_column()
    details: Mapped[dict[str, str | int]] = mapped_column(JSON)
