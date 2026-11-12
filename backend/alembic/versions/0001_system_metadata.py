"""建立系统初始化标记，验证数据库已执行基础迁移。"""

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建系统元数据并记录初始化版本。"""
    table = op.create_table(
        "system_metadata",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.bulk_insert(table, [{"key": "schema_version", "value": "1"}])


def downgrade() -> None:
    """移除初始化表，仅用于可丢弃环境的迁移验证。"""
    op.drop_table("system_metadata")
