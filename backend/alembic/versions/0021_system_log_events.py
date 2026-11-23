"""增加空间隔离的结构化诊断事件表和查询索引。"""

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str = "0020"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "system_log_events"
INDEXES = {
    "ix_system_log_workspace_time": ("workspace_id", "occurred_at", "id"),
    "ix_system_log_workspace_request": ("workspace_id", "request_id"),
    "ix_system_log_workspace_run": ("workspace_id", "run_id"),
    "ix_system_log_workspace_tool": ("workspace_id", "tool_call_id"),
    "ix_system_log_workspace_task": ("workspace_id", "task_execution_id"),
    "ix_system_log_occurred_at": ("occurred_at",),
}


def upgrade() -> None:
    """新增事件和索引，保留既有审计及运行记录。"""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("component", sa.String(16), nullable=False),
        sa.Column("event", sa.String(48), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("request_id", sa.String(32)),
        sa.Column("run_id", sa.Uuid()),
        sa.Column("tool_call_id", sa.Uuid()),
        sa.Column("connector_key", sa.String(64)),
        sa.Column("tool_name", sa.String(128)),
        sa.Column("task_execution_id", sa.Uuid()),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    for name, columns in INDEXES.items():
        op.create_index(name, TABLE, list(columns))
    op.execute("UPDATE system_metadata SET value = '21' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除独立的诊断事件表并还原就绪版本。"""
    op.drop_table(TABLE)
    op.execute("UPDATE system_metadata SET value = '20' WHERE key = 'schema_version'")
