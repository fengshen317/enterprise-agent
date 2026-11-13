"""增加集成配置表，支持前端管理模型与工具参数。

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建集成配置表。"""
    op.create_table(
        "integration_configs",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name="fk_integration_configs_updated_by_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("key", name="pk_integration_configs"),
    )
    op.execute("UPDATE system_metadata SET value = '14' WHERE key = 'schema_version'")


def downgrade() -> None:
    """删除集成配置表。"""
    op.drop_table("integration_configs")
    op.execute("UPDATE system_metadata SET value = '13' WHERE key = 'schema_version'")
