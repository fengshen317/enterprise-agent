"""为用户消息增加对话内附件和历史引用。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023"
down_revision: str = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """既有消息初始化为空附件，原件随对话级联清理。"""
    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(180), nullable=False),
        sa.Column("media_type", sa.String(110), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("extracted_text", sa.Text(), server_default="", nullable=False),
        sa.Column("text_truncated", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index("ix_chat_attachments_conversation_id", "chat_attachments", ["conversation_id"])
    op.add_column("messages", sa.Column("attachments", postgresql.JSONB(), server_default="[]", nullable=False))
    op.execute("UPDATE system_metadata SET value = '23' WHERE key = 'schema_version'")


def downgrade() -> None:
    """撤销附件扩展，保留原有会话文本。"""
    op.drop_column("messages", "attachments")
    op.drop_table("chat_attachments")
    op.execute("UPDATE system_metadata SET value = '22' WHERE key = 'schema_version'")
