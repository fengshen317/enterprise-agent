"""增加登录会话与对话归属"""

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """应用本次结构变更。"""
    op.create_table(
        "auth_login_limits",
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("failures >= 0", name=op.f("ck_auth_login_limits_failures_nonnegative")),
        sa.PrimaryKeyConstraint("key_digest", name=op.f("pk_auth_login_limits")),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_auth_sessions_expiry")),
        sa.CheckConstraint("session_version > 0", name=op.f("ck_auth_sessions_session_version_positive")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_auth_sessions_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_digest", name=op.f("uq_auth_sessions_token_digest")),
    )
    op.create_index("ix_auth_sessions_user_expiry", "auth_sessions", ["user_id", "expires_at"], unique=False)
    op.add_column("conversations", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.add_column("conversations", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_conversations_owner_workspace_updated",
        "conversations",
        ["created_by_user_id", "workspace_id", "updated_at", "id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_conversations_membership",
        "conversations",
        "workspace_memberships",
        ["workspace_id", "created_by_user_id"],
        ["workspace_id", "user_id"],
    )

    op.create_check_constraint(
        op.f("ck_conversations_ownership_pair"),
        "conversations",
        "(workspace_id IS NULL) = (created_by_user_id IS NULL)",
    )
    op.execute("UPDATE system_metadata SET value = '5' WHERE key = 'schema_version'")


def downgrade() -> None:
    """撤销本次结构变更；执行前确认数据影响。"""
    op.drop_constraint(op.f("ck_conversations_ownership_pair"), "conversations", type_="check")
    op.drop_constraint("fk_conversations_membership", "conversations", type_="foreignkey")
    op.drop_index("ix_conversations_owner_workspace_updated", table_name="conversations")
    op.drop_column("conversations", "created_by_user_id")
    op.drop_column("conversations", "workspace_id")
    op.drop_index("ix_auth_sessions_user_expiry", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("auth_login_limits")
    op.execute("UPDATE system_metadata SET value = '4' WHERE key = 'schema_version'")
