"""持久化连接器认证状态与检查证据。

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """为已有连接器保留未知认证状态，等待实际检查确认。"""
    op.add_column("connectors", sa.Column("auth_status", sa.String(20), server_default="unknown", nullable=False))
    op.add_column("connectors", sa.Column("last_error_code", sa.String(64), nullable=True))
    op.add_column("connectors", sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_connectors_auth_status",
        "connectors",
        "auth_status IN ('unknown', 'authorized', 'required', 'not_required')",
    )
    op.execute("UPDATE system_metadata SET value = '18' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除监控字段，保留连接器业务配置。"""
    op.drop_constraint("ck_connectors_auth_status", "connectors", type_="check")
    op.drop_column("connectors", "last_success_at")
    op.drop_column("connectors", "last_error_code")
    op.drop_column("connectors", "auth_status")
    op.execute("UPDATE system_metadata SET value = '17' WHERE key = 'schema_version'")
