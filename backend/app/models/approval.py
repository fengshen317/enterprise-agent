"""写操作审批与决策事件，审批绑定单次工具调用。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Approval(Base):
    """一次写操作的审批快照；参数预览已脱敏。"""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled', 'executed', 'failed')",
            name="status",
        ),
        CheckConstraint("risk_level IN ('write', 'delete', 'external')", name="risk_level"),
        UniqueConstraint("tool_call_id", name="uq_approvals_tool_call_id"),
        Index("ix_approvals_run_status_created", "run_id", "status", "requested_at"),
        Index("ix_approvals_workspace_status_expiry", "workspace_id", "status", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    tool_call_id: Mapped[UUID] = mapped_column(ForeignKey("tool_calls.id", ondelete="CASCADE"))
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[int] = mapped_column(Integer)
    risk_level: Mapped[str] = mapped_column(String(16))
    args_digest: Mapped[str] = mapped_column(String(64))
    args_preview: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    decided_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    decision_comment: Mapped[str | None] = mapped_column(String(500))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalEvent(Base):
    """审批链只追加不修改，供运行轨迹展示。"""

    __tablename__ = "approval_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('requested', 'approved', 'rejected', 'expired', 'cancelled', 'executed', 'failed')",
            name="action",
        ),
        CheckConstraint("actor_role IN ('user', 'admin', 'system')", name="actor_role"),
        Index("ix_approval_events_approval_created", "approval_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    approval_id: Mapped[UUID] = mapped_column(ForeignKey("approvals.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_role: Mapped[str] = mapped_column(String(16), default="system", server_default="system")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
