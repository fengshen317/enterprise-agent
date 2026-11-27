"""固定每轮问答使用的加密模型配置。"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """兼容历史运行，新增仅服务端读取的连接快照。"""
    op.add_column("runs", sa.Column("model_connection", sa.Text(), nullable=True))
    op.execute("UPDATE system_metadata SET value = '29' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除运行快照，保留账号保存的加密模型配置。"""
    op.drop_column("runs", "model_connection")
    op.execute("UPDATE system_metadata SET value = '28' WHERE key = 'schema_version'")
