"""增加连接器表，支持 CLI / MCP / API 外部集成。

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建连接器表。"""
    op.create_table(
        "connectors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("icon", sa.String(length=64), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("discovered_tools", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="disabled", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('cli', 'mcp', 'api')", name=op.f("ck_connectors_kind")),
        sa.CheckConstraint(
            "status IN ('disabled', 'connected', 'degraded', 'error')", name=op.f("ck_connectors_status")
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_connectors_name_nonempty")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_connectors_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connectors")),
        sa.UniqueConstraint("key", name=op.f("uq_connectors_key")),
    )
    op.create_index(op.f("ix_connectors_enabled"), "connectors", ["enabled"], unique=False)
    op.create_index(op.f("ix_connectors_kind"), "connectors", ["kind"], unique=False)
    op.execute("UPDATE system_metadata SET value = '15' WHERE key = 'schema_version'")


def downgrade() -> None:
    """删除连接器表。"""
    op.drop_index(op.f("ix_connectors_kind"), table_name="connectors")
    op.drop_index(op.f("ix_connectors_enabled"), table_name="connectors")
    op.drop_table("connectors")
    op.execute("UPDATE system_metadata SET value = '14' WHERE key = 'schema_version'")
