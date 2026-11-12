"""增加定时问答任务。

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增定时任务表；不改动已有对话和运行记录。"""
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_status", sa.String(length=20), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_scheduled_tasks_name_nonempty")),
        sa.CheckConstraint("length(trim(prompt)) > 0", name=op.f("ck_scheduled_tasks_prompt_nonempty")),
        sa.CheckConstraint("length(trim(cron_expression)) > 0", name=op.f("ck_scheduled_tasks_cron_nonempty")),
        sa.CheckConstraint("length(trim(timezone)) > 0", name=op.f("ck_scheduled_tasks_timezone_nonempty")),
        sa.CheckConstraint("NOT enabled OR next_run_at IS NOT NULL", name=op.f("ck_scheduled_tasks_enabled_next_run")),
        sa.CheckConstraint(
            "last_status IS NULL OR last_status IN ('started', 'skipped')",
            name=op.f("ck_scheduled_tasks_last_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_scheduled_tasks_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["runs.id"],
            name=op.f("fk_scheduled_tasks_last_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_scheduled_tasks_membership",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_tasks")),
    )
    op.create_index(
        "ix_scheduled_tasks_due",
        "scheduled_tasks",
        ["next_run_at"],
        unique=False,
        postgresql_where=sa.text("enabled"),
    )
    op.create_index(
        "ix_scheduled_tasks_owner_created",
        "scheduled_tasks",
        ["workspace_id", "owner_user_id", "created_at", "id"],
        unique=False,
    )
    op.execute("UPDATE system_metadata SET value = '9' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除定时任务定义；已产生的对话与运行记录不删除。"""
    op.drop_index("ix_scheduled_tasks_owner_created", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_due", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
    op.execute("UPDATE system_metadata SET value = '8' WHERE key = 'schema_version'")
