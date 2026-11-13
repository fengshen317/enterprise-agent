"""微信 iLink 渠道账号、对端会话、消息去重和审批短码。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChannelAccount(Base):
    """员工在一个空间里绑定的 iLink 机器人。token 只存密文。"""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        CheckConstraint("channel_type IN ('ilink')", name="channel_type"),
        CheckConstraint("status IN ('active', 'paused', 'unbound')", name="status"),
        Index(
            "uq_channel_accounts_user_open",
            "user_id",
            "channel_type",
            unique=True,
            postgresql_where=text("status <> 'unbound'"),
        ),
        Index("ix_channel_accounts_status", "status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    channel_type: Mapped[str] = mapped_column(String(16), default="ilink", server_default="ilink")
    ilink_bot_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    ilink_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    baseurl: Mapped[str] = mapped_column(String(300), default="", server_default="")
    bot_token_ciphertext: Mapped[str] = mapped_column(Text, default="", server_default="")
    updates_cursor: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
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
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class ChannelApprovalCode(Base):
    """推送到微信的短确认码，决策仍走原有审批服务。"""

    __tablename__ = "channel_approval_codes"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'used')", name="status"),
        Index("ix_channel_approval_codes_account_id", "account_id"),
    )
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("channel_accounts.id", ondelete="CASCADE"))
    approval_id: Mapped[UUID] = mapped_column(ForeignKey("approvals.id", ondelete="CASCADE"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
