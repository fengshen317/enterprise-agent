"""账号与工作空间成员模型，角色属于空间内成员而非全局用户。"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserStatus(StrEnum):
    """账号可用状态；禁用账号的会话由鉴权服务统一拒绝。"""

    ACTIVE = "active"
    DISABLED = "disabled"


class WorkspaceStatus(StrEnum):
    """空间归档后应由服务拒绝新生成与管理写入。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkspaceRole(StrEnum):
    """首版固定角色，不引入尚未需要的自定义权限配置。"""

    ADMIN = "admin"
    MEMBER = "member"


class MembershipStatus(StrEnum):
    """成员移除保留记录，以便后续追踪资源归属。"""

    ACTIVE = "active"
    REVOKED = "revoked"


class User(Base):
    """本地账号；密码只允许以 Argon2id 编码哈希形式持久化。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'", name="username_format"),
        CheckConstraint("display_name ~ '[^[:space:]]'", name="display_name_nonempty"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint("password_hash LIKE '$argon2id$%' AND length(password_hash) > 20", name="password_hash_format"),
        CheckConstraint("session_version > 0", name="session_version_positive"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), default=UserStatus.ACTIVE.value, server_default=UserStatus.ACTIVE.value
    )
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(Base):
    """空间是资源隔离边界；创建者字段只负责审计归属。"""

    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'", name="slug_format"),
        CheckConstraint("name ~ '[^[:space:]]'", name="name_nonempty"),
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        Index("ix_workspaces_created_by_user_id", "created_by_user_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(
        String(16), default=WorkspaceStatus.ACTIVE.value, server_default=WorkspaceStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkspaceMembership(Base):
    """用户与空间多对多关系，撤销通过状态完成，避免丢失归属记录。"""

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="role"),
        CheckConstraint("status IN ('active', 'revoked')", name="status"),
        Index("ix_workspace_memberships_user_status_workspace", "user_id", "status", "workspace_id"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(
        String(16), default=WorkspaceRole.MEMBER.value, server_default=WorkspaceRole.MEMBER.value
    )
    status: Mapped[str] = mapped_column(
        String(16), default=MembershipStatus.ACTIVE.value, server_default=MembershipStatus.ACTIVE.value
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    """服务端会话只保存令牌摘要，不保存浏览器中的原始令牌。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("session_version > 0", name="session_version_positive"),
        CheckConstraint("expires_at > created_at", name="expiry"),
        Index("ix_auth_sessions_user_expiry", "user_id", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    session_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginLimit(Base):
    """数据库维护失败窗口，后端重启不会清空同源登录尝试限制。"""

    __tablename__ = "auth_login_limits"
    __table_args__ = (CheckConstraint("failures >= 0", name="failures_nonnegative"),)
    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
