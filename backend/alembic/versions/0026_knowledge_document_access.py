"""支持知识库内受限文档的存储与读取授权。"""

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """已有文档继续从主数据集检索。"""
    op.add_column("knowledge_bases", sa.Column("restricted_dataset_id", sa.String(64)))
    op.create_unique_constraint(
        "uq_knowledge_bases_restricted_dataset_id", "knowledge_bases", ["restricted_dataset_id"]
    )
    op.add_column("knowledge_documents", sa.Column("restricted", sa.Boolean(), server_default="false", nullable=False))
    op.create_unique_constraint("uq_knowledge_documents_id_workspace", "knowledge_documents", ["id", "workspace_id"])
    op.create_index(
        "ix_knowledge_documents_restricted_kb", "knowledge_documents", ["kb_id"], postgresql_where=sa.text("restricted")
    )
    op.create_table(
        "knowledge_document_grants",
        sa.Column("document_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["knowledge_documents.id", "knowledge_documents.workspace_id"],
            name="fk_knowledge_document_grants_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_knowledge_document_grants_membership",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_knowledge_document_grants_member", "knowledge_document_grants", ["workspace_id", "user_id", "document_id"]
    )
    op.execute("UPDATE system_metadata SET value = '26' WHERE key = 'schema_version'")


def downgrade() -> None:
    """存在受限数据时拒绝回退，以免丢失其授权及原件位置。"""
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM knowledge_bases WHERE restricted_dataset_id IS NOT NULL)")):
        raise RuntimeError("存在受限数据集，回退前需保留其授权与文件映射")
    op.drop_table("knowledge_document_grants")
    op.drop_index("ix_knowledge_documents_restricted_kb", table_name="knowledge_documents")
    op.drop_constraint("uq_knowledge_documents_id_workspace", "knowledge_documents")
    op.drop_column("knowledge_documents", "restricted")
    op.drop_constraint("uq_knowledge_bases_restricted_dataset_id", "knowledge_bases")
    op.drop_column("knowledge_bases", "restricted_dataset_id")
    op.execute("UPDATE system_metadata SET value = '25' WHERE key = 'schema_version'")
