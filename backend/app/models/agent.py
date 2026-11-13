"""Agent 配置、能力绑定与工作空间工具策略。"""

from datetime import datetime
from typing import Any
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentProfile(Base):
    """可复用的 Agent 配置；“大脑”本身不含可执行代码。"""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        CheckConstraint("visibility IN ('workspace', 'private')", name="visibility"),
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_agent_profiles_membership",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_agent_profiles_id_workspace"),
        Index("ix_agent_profiles_workspace_visibility", "workspace_id", "visibility", "updated_at", "id"),
        Index("ix_agent_profiles_owner_updated", "workspace_id", "owner_user_id", "updated_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column()
    owner_user_id: Mapped[UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    icon: Mapped[str] = mapped_column(String(32), default="≋", server_default="≋")
    instructions: Mapped[str] = mapped_column(Text, default="", server_default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private", server_default="private")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    model: Mapped[str] = mapped_column(String(200), default="", server_default="")
    allow_planning: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    allow_delegation: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    knowledge_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=lambda: {"mode": "all_readable"}, server_default='{"mode":"all_readable"}'
    )
    starter_prompts: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentCapability(Base):
    """Agent 与工具/技能版本的绑定；技能版本固定，工具使用当前实现。"""

    __tablename__ = "agent_capabilities"
    __table_args__ = (
        CheckConstraint("kind IN ('tool', 'skill')", name="kind"),
        CheckConstraint("activation IN ('always', 'on_demand')", name="activation"),
        CheckConstraint(
            "(kind = 'tool' AND skill_version_id IS NULL) OR (kind = 'skill' AND skill_version_id IS NOT NULL)",
            name="target",
        ),
        UniqueConstraint("agent_profile_id", "kind", "capability_key", name="uq_agent_capabilities_target"),
        Index("ix_agent_capabilities_agent_kind", "agent_profile_id", "kind"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_profile_id: Mapped[UUID] = mapped_column(ForeignKey("agent_profiles.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    capability_key: Mapped[str] = mapped_column(String(64))
    skill_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("skill_versions.id", ondelete="SET NULL"))
    activation: Mapped[str] = mapped_column(String(16), default="always", server_default="always")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkspaceToolPolicy(Base):
    """空间管理员对内置工具的启停策略；缺省视为启用。"""

    __tablename__ = "workspace_tool_policies"
    __table_args__ = (Index("ix_workspace_tool_policies_workspace", "workspace_id", "enabled"),)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    tool_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
