"""建立用户工作空间与成员模型"""

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """应用本次结构变更。"""
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("session_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("display_name ~ '[^[:space:]]'", name=op.f("ck_users_display_name_nonempty")),
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%' AND length(password_hash) > 20",
            name=op.f("ck_users_password_hash_format"),
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_users_status")),
        sa.CheckConstraint("username ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'", name=op.f("ck_users_username_format")),
        sa.CheckConstraint("session_version > 0", name=op.f("ck_users_session_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name=op.f("ck_workspaces_name_nonempty")),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'", name=op.f("ck_workspaces_slug_format")),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_workspaces_status")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name=op.f("fk_workspaces_created_by_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspaces_slug")),
    )
    op.create_index("ix_workspaces_created_by_user_id", "workspaces", ["created_by_user_id"], unique=False)
    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), server_default="member", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member')", name=op.f("ck_workspace_memberships_role")),
        sa.CheckConstraint("status IN ('active', 'revoked')", name=op.f("ck_workspace_memberships_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_workspace_memberships_user_id_users")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_workspace_memberships_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_workspace_memberships")),
    )
    op.create_index(
        "ix_workspace_memberships_user_status_workspace",
        "workspace_memberships",
        ["user_id", "status", "workspace_id"],
        unique=False,
    )

    op.execute("UPDATE system_metadata SET value = '4' WHERE key = 'schema_version'")


def downgrade() -> None:
    """撤销本次结构变更；执行前确认数据影响。"""
    op.execute("UPDATE system_metadata SET value = '3' WHERE key = 'schema_version'")
    op.drop_index("ix_workspace_memberships_user_status_workspace", table_name="workspace_memberships")
    op.drop_table("workspace_memberships")
    op.drop_index("ix_workspaces_created_by_user_id", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_table("users")
