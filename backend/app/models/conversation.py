"""问答持久化模型；业务记录与 Agent 检查点分别管理。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Conversation(Base):
    """交流容器；普通问答不会隐式创建业务任务。"""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        CheckConstraint("next_message_sequence > 0", name="next_sequence_positive"),
        Index("ix_conversations_updated_at_id", "updated_at", "id"),
        Index("ix_conversations_owner_workspace_updated", "created_by_user_id", "workspace_id", "updated_at", "id"),
        Index("ix_conversations_agent", "workspace_id", "agent_profile_id"),
        CheckConstraint("(workspace_id IS NULL) = (created_by_user_id IS NULL)", name="ownership_pair"),
        ForeignKeyConstraint(
            ["workspace_id", "created_by_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_conversations_membership",
        ),
        CheckConstraint("summary_through_sequence >= 0 AND summary_through_sequence % 2 = 0", name="summary_sequence"),
        CheckConstraint(
            "(summary_through_sequence = 0 AND summary = '' AND summary_version = 0 "
            "AND summary_updated_at IS NULL AND summary_model IS NULL) OR "
            "(summary_through_sequence > 0 AND length(summary) > 0 AND summary_version > 0 "
            "AND summary_updated_at IS NOT NULL AND summary_model IS NOT NULL)",
            name="summary_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID | None] = mapped_column()
    created_by_user_id: Mapped[UUID | None] = mapped_column()
    agent_profile_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_profiles.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    summary_through_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    summary_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_model: Mapped[str | None] = mapped_column(String(200))
    next_message_sequence: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    """一次生成尝试；失败后重试应创建新记录。"""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "idempotency_key", name="uq_runs_conversation_id_idempotency_key"),
        UniqueConstraint("conversation_id", "id", name="uq_runs_conversation_id_id"),
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name="status"),
        CheckConstraint("input_sequence > 0", name="input_sequence_positive"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_nonempty"),
        CheckConstraint(
            "(status IN ('pending', 'running') AND finished_at IS NULL) OR "
            "(status IN ('completed', 'failed', 'cancelled') AND finished_at IS NOT NULL)",
            name="terminal_timestamp",
        ),
        Index(
            "uq_runs_one_active_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_runs_conversation_id_created_at", "conversation_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"))
    agent_profile_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_profiles.id", ondelete="SET NULL"))
    agent_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    model: Mapped[str] = mapped_column(String(200))
    input_sequence: Mapped[int] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    """一条完整消息；流式内容逐批更新同一条记录。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_id_sequence"),
        UniqueConstraint("run_id", name="uq_messages_run_id"),
        ForeignKeyConstraint(
            ["conversation_id", "run_id"], ["runs.conversation_id", "runs.id"], name="fk_messages_conversation_run"
        ),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("status IN ('streaming', 'completed', 'interrupted')", name="status"),
        CheckConstraint(
            "(role = 'user' AND run_id IS NULL AND status = 'completed') OR "
            "(role = 'assistant' AND run_id IS NOT NULL)",
            name="role_run_binding",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"))
    run_id: Mapped[UUID | None] = mapped_column()
    sequence: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
