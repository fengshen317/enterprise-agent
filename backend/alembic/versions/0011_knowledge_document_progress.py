"""增加知识库文档解析进度和失败详情。

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """补充文档进度与可持久化的失败说明。"""
    op.add_column(
        "knowledge_documents",
        sa.Column("progress", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.execute("UPDATE system_metadata SET value = '11' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除新增列；已有文档状态不受影响。"""
    op.drop_column("knowledge_documents", "error_message")
    op.drop_column("knowledge_documents", "progress")
    op.execute("UPDATE system_metadata SET value = '10' WHERE key = 'schema_version'")
