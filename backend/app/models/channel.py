"""渠道账号、企业机器人凭证、对端会话、消息去重和审批短码。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChannelApp(Base):
    """企业安装的一个飞书、钉钉或 QQ 机器人。密钥只存密文。"""

    __tablename__ = "channel_apps"
    __table_args__ = (
        CheckConstraint("channel_type IN ('feishu', 'dingtalk', 'qq')", name="channel_type"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        Index(
            "uq_channel_apps_type_active",
            "channel_type",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # 只记录是谁配的。删空间不能把全公司正在用的机器人一起删掉。
    workspace_id: Mapped[UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))
    channel_type: Mapped[str] = mapped_column(String(16))
    external_app_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    credentials_ciphertext: Mapped[str] = mapped_column(Text, default="", server_default="")
    public_config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, server_default=text("'{}'::json"))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelAccount(Base):
    """员工绑定的渠道身份。微信的 token 在本行，企业机器人的密钥在 channel_apps。"""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        CheckConstraint("channel_type IN ('ilink', 'feishu', 'dingtalk', 'qq')", name="channel_type"),
        CheckConstraint("status IN ('active', 'paused', 'unbound')", name="status"),
        Index(
            "uq_channel_accounts_user_open",
            "user_id",
            "channel_type",
            unique=True,
            postgresql_where=text("status <> 'unbound'"),
        ),
        Index(
            "uq_channel_accounts_platform_user",
            "channel_type",
            "ilink_user_id",
            unique=True,
            postgresql_where=text("status <> 'unbound' AND ilink_user_id <> ''"),
        ),
        Index("ix_channel_accounts_status", "status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    channel_type: Mapped[str] = mapped_column(String(16), default="ilink", server_default="ilink")
    ilink_bot_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    ilink_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    app_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_apps.id", ondelete="SET NULL"))
    baseurl: Mapped[str] = mapped_column(String(300), default="", server_default="")
    bot_token_ciphertext: Mapped[str] = mapped_column(Text, default="", server_default="")
    updates_cursor: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    welcome_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelBindSession(Base):
    """扫码完成前的临时会话，确认后才写入 channel_accounts。"""

    __tablename__ = "channel_bind_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'confirmed', 'expired', 'failed')", name="status"),
        CheckConstraint("refresh_count >= 0", name="refresh_count_nonnegative"),
        Index("ix_channel_bind_sessions_user_id", "user_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    qrcode: Mapped[str] = mapped_column(Text)
    qrcode_img_content: Mapped[str] = mapped_column(Text, default="", server_default="")
    baseurl: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    refresh_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_accounts.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelPeer(Base):
    """微信用户在某个空间中对应的当前对话，以及 24 小时窗口。"""

    __tablename__ = "channel_peers"
    __table_args__ = (
        UniqueConstraint("account_id", "peer_id", "workspace_id", name="uq_channel_peers_account_peer_workspace"),
        Index("ix_channel_peers_account_inbound", "account_id", "last_inbound_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("channel_accounts.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    peer_id: Mapped[str] = mapped_column(String(128))
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"))
    context_token: Mapped[str] = mapped_column(Text, default="", server_default="")
    passive_message_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    passive_reply_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_for_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelMessage(Base):
    """入站去重和出站结果。不保存完整正文，只留短摘录。"""

    __tablename__ = "channel_messages"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("status IN ('received', 'skipped', 'sent', 'failed')", name="status"),
        UniqueConstraint("account_id", "ilink_message_id", name="uq_channel_messages_account_message"),
        Index("ix_channel_messages_run_id", "run_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("channel_accounts.id", ondelete="CASCADE"))
    peer_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    direction: Mapped[str] = mapped_column(String(16))
    ilink_message_id: Mapped[str | None] = mapped_column(String(80))
    client_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16))
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    body_excerpt: Mapped[str] = mapped_column(String(500), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelBindCode(Base):
    """员工在单聊里发送的一次性绑定码，用完即失效。"""

    __tablename__ = "channel_bind_codes"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'used', 'expired')", name="status"),
        Index(
            "uq_channel_bind_codes_pending",
            "code",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_channel_bind_codes_user_id", "user_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    app_id: Mapped[UUID] = mapped_column(ForeignKey("channel_apps.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelAppEvent(Base):
    """还没有绑定工作台账号时的入站审计。不拿它去猜测用户。"""

    __tablename__ = "channel_app_events"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("status IN ('received', 'skipped', 'sent', 'failed')", name="status"),
        UniqueConstraint("app_id", "message_id", name="uq_channel_app_events_message"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    app_id: Mapped[UUID] = mapped_column(ForeignKey("channel_apps.id", ondelete="CASCADE"))
    peer_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    direction: Mapped[str] = mapped_column(String(16))
    message_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    skip_reason: Mapped[str | None] = mapped_column(String(64))
    body_excerpt: Mapped[str] = mapped_column(String(500), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelApprovalCode(Base):
    """推送到微信的短确认码，决策仍走原有审批服务。"""

    __tablename__ = "channel_approval_codes"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'used')", name="status"),
        CheckConstraint("risk_level IN ('write', 'delete', 'external')", name="risk_level"),
        Index("ix_channel_approval_codes_account_id", "account_id"),
    )
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("channel_accounts.id", ondelete="CASCADE"))
    approval_id: Mapped[UUID] = mapped_column(ForeignKey("approvals.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    risk_level: Mapped[str] = mapped_column(String(16), default="write", server_default="write")
    tool_name: Mapped[str] = mapped_column(String(64), default="", server_default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
