"""增加写操作审批、决策事件与工具调用风险字段。

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """增加审批表并扩展工具审计状态。"""
    op.add_column("tool_calls", sa.Column("tool_version", sa.Integer(), nullable=True))
    op.add_column("tool_calls", sa.Column("risk_level", sa.String(length=16), nullable=True))
    op.add_column(
        "tool_calls",
        sa.Column("approval_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("tool_calls", sa.Column("arguments_digest", sa.String(length=64), nullable=True))
    op.drop_constraint(op.f("ck_tool_calls_status"), "tool_calls", type_="check")
    op.drop_constraint(op.f("ck_tool_calls_terminal_timestamp"), "tool_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_tool_calls_status"),
        "tool_calls",
        "status IN ('awaiting_approval', 'running', 'completed', 'failed', 'cancelled', 'denied', 'expired')",
    )
    op.create_check_constraint(
        op.f("ck_tool_calls_terminal_timestamp"),
        "tool_calls",
        "(status IN ('awaiting_approval', 'running') AND finished_at IS NULL) OR "
        "(status NOT IN ('awaiting_approval', 'running') AND finished_at IS NOT NULL)",
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("args_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "args_preview",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decision_comment", sa.String(length=500), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled', 'executed', 'failed')",
            name=op.f("ck_approvals_status"),
        ),
        sa.CheckConstraint("risk_level IN ('write', 'delete', 'external')", name=op.f("ck_approvals_risk_level")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_approvals_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name="fk_approvals_run_id_runs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_calls.id"],
            name="fk_approvals_tool_call_id_tool_calls",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_approvals_requested_by_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name="fk_approvals_decided_by_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("tool_call_id", name="uq_approvals_tool_call_id"),
    )
    op.create_index(
        "ix_approvals_run_status_created",
        "approvals",
        ["run_id", "status", "requested_at"],
        unique=False,
    )
    op.create_index(
        "ix_approvals_workspace_status_expiry",
        "approvals",
        ["workspace_id", "status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "approval_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_role", sa.String(length=16), server_default="system", nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action IN ('requested', 'approved', 'rejected', 'expired', 'cancelled', 'executed', 'failed')",
            name=op.f("ck_approval_events_action"),
        ),
        sa.CheckConstraint("actor_role IN ('user', 'admin', 'system')", name=op.f("ck_approval_events_actor_role")),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name="fk_approval_events_approval_id_approvals",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_approval_events_actor_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_events")),
    )
    op.create_index(
        "ix_approval_events_approval_created",
        "approval_events",
        ["approval_id", "created_at"],
        unique=False,
    )
    op.execute("UPDATE system_metadata SET value = '13' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除审批表并恢复旧工具状态约束。"""
    op.drop_index("ix_approval_events_approval_created", table_name="approval_events")
    op.drop_table("approval_events")
    op.drop_index("ix_approvals_workspace_status_expiry", table_name="approvals")
    op.drop_index("ix_approvals_run_status_created", table_name="approvals")
    op.drop_table("approvals")
    op.drop_constraint(op.f("ck_tool_calls_terminal_timestamp"), "tool_calls", type_="check")
    op.drop_constraint(op.f("ck_tool_calls_status"), "tool_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_tool_calls_status"),
        "tool_calls",
        "status IN ('running', 'completed', 'failed', 'cancelled')",
    )
    op.create_check_constraint(
        op.f("ck_tool_calls_terminal_timestamp"),
        "tool_calls",
        "(status = 'running' AND finished_at IS NULL) OR (status != 'running' AND finished_at IS NOT NULL)",
    )
    op.drop_column("tool_calls", "arguments_digest")
    op.drop_column("tool_calls", "approval_required")
    op.drop_column("tool_calls", "risk_level")
    op.drop_column("tool_calls", "tool_version")
    op.execute("UPDATE system_metadata SET value = '12' WHERE key = 'schema_version'")
