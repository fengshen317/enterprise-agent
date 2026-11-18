"""为定时任务增加逐任务微信推送开关。

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """已有任务默认不外发，只有用户主动开启后才推送微信。"""
    op.add_column(
        "scheduled_tasks",
        sa.Column("push_to_wechat", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE system_metadata SET value = '19' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除逐任务推送开关。"""
    op.drop_column("scheduled_tasks", "push_to_wechat")
    op.execute("UPDATE system_metadata SET value = '18' WHERE key = 'schema_version'")
