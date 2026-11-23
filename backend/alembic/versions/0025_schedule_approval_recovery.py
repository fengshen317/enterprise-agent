"""保存创建定时任务所需的审批快照。"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史审批保留空值，避免重放无法验证的操作。"""
    op.add_column("approvals", sa.Column("execution_args", JSONB(), nullable=True))
    op.execute("UPDATE system_metadata SET value = '25' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除恢复参数，保留已有任务和审批历史。"""
    op.drop_column("approvals", "execution_args")
    op.execute("UPDATE system_metadata SET value = '24' WHERE key = 'schema_version'")
