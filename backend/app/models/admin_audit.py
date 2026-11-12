"""空间管理审计，不保存密码或会话。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminAudit(Base):
    """操作身份以快照保留，归属空间统一控制读取权限。"""

    __tablename__ = "admin_audits"
    __table_args__ = (Index("ix_admin_audits_workspace_created", "workspace_id", "created_at", "id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_user_id: Mapped[UUID] = mapped_column()
    target_user_id: Mapped[UUID] = mapped_column()
    actor_username: Mapped[str] = mapped_column(String(64))
    target_username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    before_role: Mapped[str | None] = mapped_column(String(16))
    before_status: Mapped[str | None] = mapped_column(String(16))
    after_role: Mapped[str] = mapped_column(String(16))
    after_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
