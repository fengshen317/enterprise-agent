"""保存可跨运行复用的对话摘要，保留全部原始消息。"""

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增摘要状态，已有对话以空摘要开始。"""
    op.add_column("conversations", sa.Column("summary", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "conversations", sa.Column("summary_through_sequence", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column("conversations", sa.Column("summary_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("conversations", sa.Column("summary_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("summary_model", sa.String(200), nullable=True))
    op.create_check_constraint(
        op.f("ck_conversations_summary_sequence"),
        "conversations",
        "summary_through_sequence >= 0 AND summary_through_sequence % 2 = 0",
    )
    op.create_check_constraint(
        op.f("ck_conversations_summary_state"),
        "conversations",
        "(summary_through_sequence = 0 AND summary = '' AND summary_version = 0 "
        "AND summary_updated_at IS NULL AND summary_model IS NULL) OR "
        "(summary_through_sequence > 0 AND length(summary) > 0 AND summary_version > 0 "
        "AND summary_updated_at IS NOT NULL AND summary_model IS NOT NULL)",
    )
    op.execute("UPDATE system_metadata SET value = '3' WHERE key = 'schema_version'")


def downgrade() -> None:
    """仅移除派生摘要，不删除原始消息；需回到旧版应用再启动。"""
    op.drop_constraint(op.f("ck_conversations_summary_state"), "conversations", type_="check")
    op.drop_constraint(op.f("ck_conversations_summary_sequence"), "conversations", type_="check")
    for name in ("summary_model", "summary_updated_at", "summary_version", "summary_through_sequence", "summary"):
        op.drop_column("conversations", name)
    op.execute("UPDATE system_metadata SET value = '2' WHERE key = 'schema_version'")
