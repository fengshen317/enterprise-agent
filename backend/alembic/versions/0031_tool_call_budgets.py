"""区分系统动作和业务调用，并记录实际获准占用的预算。"""

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留历史审计，同时让并发预算计数不包含被提前拒绝的调用。"""
    op.add_column("tool_calls", sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("tool_calls", sa.Column("budget_reserved", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.execute(
        "UPDATE tool_calls SET is_system = true WHERE tool_name IN "
        "('model_request', 'delegate_agent', 'update_plan', 'delegate_tasks', 'load_skill')"
    )
    op.execute(
        "UPDATE tool_calls SET budget_reserved = false WHERE error_code IN "
        "('TOOL_CALL_LIMIT', 'MODEL_CALL_LIMIT', 'DELEGATION_LIMIT')"
    )
    op.execute("UPDATE system_metadata SET value = '31' WHERE key = 'schema_version'")


def downgrade() -> None:
    """回退预算分类，保留原有工具执行记录。"""
    op.drop_column("tool_calls", "budget_reserved")
    op.drop_column("tool_calls", "is_system")
    op.execute("UPDATE system_metadata SET value = '30' WHERE key = 'schema_version'")
