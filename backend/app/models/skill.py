"""技能与不可变版本；技能正文是工作指令，不是可执行代码。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Skill(Base):
    """版本化技能的身份与发布状态。"""

    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("visibility IN ('workspace', 'private')", name="visibility"),
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status"),
        CheckConstraint("current_version > 0", name="current_version_positive"),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("key ~ '^[a-z0-9][a-z0-9_-]{1,63}$'", name="key_format"),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_skills_membership",
        ),
        UniqueConstraint("workspace_id", "key", name="uq_skills_workspace_key"),
        UniqueConstraint("id", "workspace_id", name="uq_skills_id_workspace"),
        Index("ix_skills_workspace_visibility", "workspace_id", "visibility", "updated_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column()
    owner_user_id: Mapped[UUID] = mapped_column()
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private", server_default="private")
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SkillVersion(Base):
    """不可编辑的技能版本，历史运行与 Agent 绑定可长期追溯。"""

    __tablename__ = "skill_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("activation IN ('always', 'on_demand')", name="activation"),
        CheckConstraint("token_budget > 0", name="token_budget_positive"),
        CheckConstraint("length(content_md) > 0", name="content_nonempty"),
        UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
        Index("ix_skill_versions_skill_created", "skill_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    content_md: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    tool_keys: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    knowledge_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=lambda: {"mode": "all_readable"}, server_default='{"mode":"all_readable"}'
    )
    activation: Mapped[str] = mapped_column(String(16), default="always", server_default="always")
    token_budget: Mapped[int] = mapped_column(Integer, default=4000, server_default="4000")
    change_note: Mapped[str] = mapped_column(String(500), default="", server_default="")
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
