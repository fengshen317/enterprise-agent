"""增加只读工具审计，保留已有问答与会话。

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增审计表并推进就绪标记。"""
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), server_default="running", nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')", name=op.f("ck_tool_calls_status")
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR (status != 'running' AND finished_at IS NOT NULL)",
            name=op.f("ck_tool_calls_terminal_timestamp"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name=op.f("fk_tool_calls_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_run_created", "tool_calls", ["run_id", "created_at", "id"])
    op.execute("UPDATE system_metadata SET value = '6' WHERE key = 'schema_version'")


def downgrade() -> None:
    """只移除工具审计，原问答与会话结构不变。"""
    op.drop_index("ix_tool_calls_run_created", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.execute("UPDATE system_metadata SET value = '5' WHERE key = 'schema_version'")
