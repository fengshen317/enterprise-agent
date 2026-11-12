"""增加空间成员管理审计。

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增审计表，不修改原用户与成员角色。"""
    op.create_table(
        "admin_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("target_username", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("before_role", sa.String(16), nullable=True),
        sa.Column("before_status", sa.String(16), nullable=True),
        sa.Column("after_role", sa.String(16), nullable=False),
        sa.Column("after_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name=op.f("fk_admin_audits_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audits")),
    )
    op.create_index("ix_admin_audits_workspace_created", "admin_audits", ["workspace_id", "created_at", "id"])
    op.execute("UPDATE system_metadata SET value = '7' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除本次审计；实际成员变更不会被还原。"""
    op.drop_index("ix_admin_audits_workspace_created", table_name="admin_audits")
    op.drop_table("admin_audits")
    op.execute("UPDATE system_metadata SET value = '6' WHERE key = 'schema_version'")
