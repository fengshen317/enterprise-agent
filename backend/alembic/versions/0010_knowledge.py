"""增加知识库与文档元数据。

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增知识库表和文档表，不修改已有问答数据。"""
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("visibility", sa.String(length=16), server_default="workspace", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="creating", nullable=False),
        sa.Column("ragflow_dataset_id", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=200), server_default="", nullable=False),
        sa.Column("parser_id", sa.String(length=64), server_default="general", nullable=False),
        sa.Column("parse_type", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "parser_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("visibility IN ('workspace', 'private')", name=op.f("ck_knowledge_bases_visibility")),
        sa.CheckConstraint(
            "status IN ('creating', 'ready', 'failed', 'configuring', 'deleting', 'delete_failed')",
            name=op.f("ck_knowledge_bases_status"),
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_knowledge_bases_name_nonempty")),
        sa.CheckConstraint("config_version > 0", name=op.f("ck_knowledge_bases_config_version_positive")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_knowledge_bases_membership",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
        sa.UniqueConstraint("id", "workspace_id", name="uq_knowledge_bases_id_workspace"),
        sa.UniqueConstraint("ragflow_dataset_id", name="uq_knowledge_bases_ragflow_dataset_id"),
    )
    op.create_index(
        "ix_knowledge_bases_owner_updated",
        "knowledge_bases",
        ["workspace_id", "owner_user_id", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_bases_workspace_visibility",
        "knowledge_bases",
        ["workspace_id", "visibility", "updated_at", "id"],
        unique=False,
    )
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("ragflow_document_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=100), server_default="application/octet-stream", nullable=False),
        sa.Column("size", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="uploading", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "parse_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("parse_config_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('uploading', 'uploaded', 'configuring', 'parsing', 'ready', 'failed', "
            "'deleting', 'delete_failed')",
            name=op.f("ck_knowledge_documents_status"),
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_knowledge_documents_name_nonempty")),
        sa.CheckConstraint("size >= 0", name=op.f("ck_knowledge_documents_size_nonnegative")),
        sa.CheckConstraint(
            "chunk_count >= 0 AND token_count >= 0", name=op.f("ck_knowledge_documents_counts_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["kb_id", "workspace_id"],
            ["knowledge_bases.id", "knowledge_bases.workspace_id"],
            name="fk_knowledge_documents_kb_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f("fk_knowledge_documents_uploaded_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
        sa.UniqueConstraint("ragflow_document_id", name="uq_knowledge_documents_ragflow_document_id"),
    )
    op.create_index(
        "ix_knowledge_documents_kb_created",
        "knowledge_documents",
        ["kb_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_documents_workspace_status",
        "knowledge_documents",
        ["workspace_id", "status", "updated_at"],
        unique=False,
    )
    op.execute("UPDATE system_metadata SET value = '10' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除知识库元数据；RAGFlow 中的 dataset 不由迁移删除。"""
    op.drop_index("ix_knowledge_documents_workspace_status", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_kb_created", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index("ix_knowledge_bases_workspace_visibility", table_name="knowledge_bases")
    op.drop_index("ix_knowledge_bases_owner_updated", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
    op.execute("UPDATE system_metadata SET value = '9' WHERE key = 'schema_version'")
