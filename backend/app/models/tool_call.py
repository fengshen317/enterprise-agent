"""只读工具调用审计，归属统一继承运行。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ToolCall(Base):
    """保存安全参数和聚合结果，不保存模型的原始任意参数。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_approval', 'running', 'completed', 'failed', 'cancelled', 'denied', 'expired')",
            name="status",
        ),
        CheckConstraint(
            "(status IN ('awaiting_approval', 'running') AND finished_at IS NULL) OR "
            "(status NOT IN ('awaiting_approval', 'running') AND finished_at IS NOT NULL)",
            name="terminal_timestamp",
        ),
        Index("ix_tool_calls_run_created", "run_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[int | None] = mapped_column(Integer)
    risk_level: Mapped[str | None] = mapped_column(String(16))
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    arguments_digest: Mapped[str | None] = mapped_column(String(64))
    days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="running", server_default="running")
    result: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
