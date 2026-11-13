"""建立问答对话消息与运行模型"""

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """应用本次结构变更。"""
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("next_message_sequence", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_conversations_status")),
        sa.CheckConstraint("next_message_sequence > 0", name=op.f("ck_conversations_next_sequence_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index("ix_conversations_updated_at_id", "conversations", ["updated_at", "id"], unique=False)
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("input_sequence", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status IN ('pending', 'running') AND finished_at IS NULL) OR "
            "(status IN ('completed', 'failed', 'cancelled') AND finished_at IS NOT NULL)",
            name=op.f("ck_runs_terminal_timestamp"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name=op.f("ck_runs_status")
        ),
        sa.CheckConstraint("input_sequence > 0", name=op.f("ck_runs_input_sequence_positive")),
        sa.CheckConstraint("length(idempotency_key) > 0", name=op.f("ck_runs_idempotency_key_nonempty")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], name=op.f("fk_runs_conversation_id_conversations")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("conversation_id", "id", name="uq_runs_conversation_id_id"),
        sa.UniqueConstraint("conversation_id", "idempotency_key", name="uq_runs_conversation_id_idempotency_key"),
    )
    op.create_index("ix_runs_conversation_id_created_at", "runs", ["conversation_id", "created_at"], unique=False)
    op.create_index(
        "uq_runs_one_active_per_conversation",
        "runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(role = 'user' AND run_id IS NULL AND status = 'completed') OR "
            "(role = 'assistant' AND run_id IS NOT NULL)",
            name=op.f("ck_messages_role_run_binding"),
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role")),
        sa.CheckConstraint("status IN ('streaming', 'completed', 'interrupted')", name=op.f("ck_messages_status")),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_messages_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["conversation_id", "run_id"], ["runs.conversation_id", "runs.id"], name="fk_messages_conversation_run"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], name=op.f("fk_messages_conversation_id_conversations")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_id_sequence"),
        sa.UniqueConstraint("run_id", name="uq_messages_run_id"),
    )

    op.execute("UPDATE system_metadata SET value = '2' WHERE key = 'schema_version'")


def downgrade() -> None:
    """撤销本次结构变更；执行前确认数据影响。"""
    op.execute("UPDATE system_metadata SET value = '1' WHERE key = 'schema_version'")
    op.drop_table("messages")
    op.drop_index(
        "uq_runs_one_active_per_conversation",
        table_name="runs",
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.drop_index("ix_runs_conversation_id_created_at", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_conversations_updated_at_id", table_name="conversations")
    op.drop_table("conversations")
