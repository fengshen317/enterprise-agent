"""为定时问答增加日历筛选和配置版本。"""

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str = "0021"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "scheduled_tasks"


def upgrade() -> None:
    """旧任务默认按原 Cron 触发，并从版本一开始。"""
    op.add_column(TABLE, sa.Column("calendar_mode", sa.String(16), server_default="all_days", nullable=False))
    op.add_column(TABLE, sa.Column("calendar_waiting_year", sa.Integer()))
    op.add_column(TABLE, sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.drop_constraint(op.f("ck_scheduled_tasks_enabled_next_run"), TABLE, type_="check")
    op.create_check_constraint(
        "enabled_next_run", TABLE, "NOT enabled OR next_run_at IS NOT NULL OR calendar_waiting_year IS NOT NULL"
    )
    op.create_check_constraint("calendar_mode", TABLE, "calendar_mode IN ('all_days', 'cn_workday', 'cn_a_share')")
    op.create_check_constraint(
        "calendar_waiting", TABLE,
        "calendar_waiting_year IS NULL OR (enabled AND next_run_at IS NULL AND calendar_mode != 'all_days')",
    )
    op.execute("UPDATE system_metadata SET value = '22' WHERE key = 'schema_version'")


def downgrade() -> None:
    """保留任务定义，等待公告的任务在旧版本中转为停用。"""
    op.drop_constraint(op.f("ck_scheduled_tasks_calendar_waiting"), TABLE, type_="check")
    op.execute("UPDATE scheduled_tasks SET enabled = false WHERE calendar_waiting_year IS NOT NULL")
    op.drop_constraint(op.f("ck_scheduled_tasks_enabled_next_run"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_scheduled_tasks_calendar_mode"), TABLE, type_="check")
    op.create_check_constraint("enabled_next_run", TABLE, "NOT enabled OR next_run_at IS NOT NULL")
    op.drop_column(TABLE, "revision")
    op.drop_column(TABLE, "calendar_waiting_year")
    op.drop_column(TABLE, "calendar_mode")
    op.execute("UPDATE system_metadata SET value = '21' WHERE key = 'schema_version'")
