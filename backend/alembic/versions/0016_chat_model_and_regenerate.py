"""对话级模型、上下文用量、思考过程与原地重新生成。

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """增加模型选择、用量、推理文本和回答替代所需字段。"""
    op.add_column("conversations", sa.Column("model", sa.String(length=200), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("context_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("messages", sa.Column("model", sa.String(length=200), nullable=True))
    op.add_column("messages", sa.Column("reasoning", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("phase", sa.String(length=20), nullable=True))
    op.add_column("runs", sa.Column("superseded_by_run_id", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("archived_content", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("archived_reasoning", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("archived_prompt", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_runs_phase",
        "runs",
        "phase IS NULL OR phase IN ('compressing', 'generating')",
    )
    op.create_foreign_key(
        "fk_runs_superseded_by_run_id_runs",
        "runs",
        "runs",
        ["superseded_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE messages AS message
        SET model = run.model
        FROM runs AS run
        WHERE message.run_id = run.id AND message.role = 'assistant' AND message.model IS NULL
        """
    )


def downgrade() -> None:
    """移除本迁移新增的列与约束，不删除消息正文。"""
    op.drop_constraint("fk_runs_superseded_by_run_id_runs", "runs", type_="foreignkey")
    op.drop_constraint("ck_runs_phase", "runs", type_="check")
    op.drop_column("runs", "prompt_tokens")
    op.drop_column("runs", "archived_prompt")
    op.drop_column("runs", "archived_reasoning")
    op.drop_column("runs", "archived_content")
    op.drop_column("runs", "superseded_by_run_id")
    op.drop_column("runs", "phase")
    op.drop_column("messages", "reasoning")
    op.drop_column("messages", "model")
    op.drop_column("conversations", "context_usage")
    op.drop_column("conversations", "model")
