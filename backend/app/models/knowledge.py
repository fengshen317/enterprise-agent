"""知识库与文档元数据；权限归属由应用数据库维护。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
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


class KnowledgeBase(Base):
    """一个应用知识库对应一个 RAGFlow dataset。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('workspace', 'private')",
            name="visibility",
        ),
        CheckConstraint(
            "status IN ('creating', 'ready', 'failed', 'configuring', 'deleting', 'delete_failed')",
            name="status",
        ),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("config_version > 0", name="config_version_positive"),
        ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_knowledge_bases_membership",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_knowledge_bases_id_workspace"),
        Index("ix_knowledge_bases_owner_updated", "workspace_id", "owner_user_id", "updated_at", "id"),
        Index("ix_knowledge_bases_workspace_visibility", "workspace_id", "visibility", "updated_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column()
    owner_user_id: Mapped[UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    visibility: Mapped[str] = mapped_column(String(16), default="workspace", server_default="workspace")
    status: Mapped[str] = mapped_column(String(20), default="creating", server_default="creating")
    ragflow_dataset_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    embedding_model: Mapped[str] = mapped_column(String(200), default="", server_default="")
    parser_id: Mapped[str] = mapped_column(String(64), default="general", server_default="general")
    parse_type: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    parser_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeDocument(Base):
    """原始文档元数据；RAGFlow 文档 ID 不返回前端。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'uploaded', 'configuring', 'parsing', 'ready', 'failed', "
            "'deleting', 'delete_failed')",
            name="status",
        ),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("size >= 0", name="size_nonnegative"),
        CheckConstraint("chunk_count >= 0 AND token_count >= 0", name="counts_nonnegative"),
        ForeignKeyConstraint(
            ["kb_id", "workspace_id"],
            ["knowledge_bases.id", "knowledge_bases.workspace_id"],
            name="fk_knowledge_documents_kb_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_documents_kb_created", "kb_id", "created_at", "id"),
        Index("ix_knowledge_documents_workspace_status", "workspace_id", "status", "updated_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kb_id: Mapped[UUID] = mapped_column()
    workspace_id: Mapped[UUID] = mapped_column()
    uploaded_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    ragflow_document_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="uploading", server_default="uploading")
    error_code: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text)
    parse_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    parse_config_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
