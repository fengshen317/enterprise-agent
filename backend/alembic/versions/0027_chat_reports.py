"""保存问答数据分析报表。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建随助手消息清理的报表及其引用索引。"""
    op.create_table(
        "chat_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("source_files", postgresql.JSONB(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chat_reports_message_id"), "chat_reports", ["message_id"])
    op.execute("UPDATE system_metadata SET value = '27' WHERE key = 'schema_version'")


def downgrade() -> None:
    """恢复到不存储报表的模式。"""
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM chat_reports)")):
        raise RuntimeError("已存在聊天报表，需保留报表数据后再回退")
    op.drop_table("chat_reports")
    op.execute("UPDATE system_metadata SET value = '26' WHERE key = 'schema_version'")
