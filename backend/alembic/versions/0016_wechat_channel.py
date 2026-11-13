"""增加微信 iLink 渠道表。

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建渠道账号、绑定会话、对端、消息和审批短码。"""
    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("channel_type", sa.String(length=16), server_default="ilink", nullable=False),
        sa.Column("ilink_bot_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("ilink_user_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("baseurl", sa.String(length=300), server_default="", nullable=False),
        sa.Column("bot_token_ciphertext", sa.Text(), server_default="", nullable=False),
        sa.Column("updates_cursor", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("channel_type IN ('ilink')", name=op.f("ck_channel_accounts_channel_type")),
        sa.CheckConstraint("status IN ('active', 'paused', 'unbound')", name=op.f("ck_channel_accounts_status")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_channel_accounts_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_channel_accounts_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_accounts")),
    )
    op.create_index("ix_channel_accounts_status", "channel_accounts", ["status"], unique=False)
    op.create_index(
        "uq_channel_accounts_user_open",
        "channel_accounts",
        ["user_id", "channel_type"],
        unique=True,
        postgresql_where=sa.text("status <> 'unbound'"),
    )
    op.create_table(
        "channel_bind_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("qrcode", sa.Text(), nullable=False),
        sa.Column("qrcode_img_content", sa.Text(), server_default="", nullable=False),
        sa.Column("baseurl", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("refresh_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'expired', 'failed')",
            name=op.f("ck_channel_bind_sessions_status"),
        ),
        sa.CheckConstraint("refresh_count >= 0", name=op.f("ck_channel_bind_sessions_refresh_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_channel_bind_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_channel_bind_sessions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["channel_accounts.id"],
            name=op.f("fk_channel_bind_sessions_account_id_channel_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_bind_sessions")),
    )
    op.create_index("ix_channel_bind_sessions_user_id", "channel_bind_sessions", ["user_id"], unique=False)
    op.create_table(
        "channel_peers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("peer_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("context_token", sa.Text(), server_default="", nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["channel_accounts.id"],
            name=op.f("fk_channel_peers_account_id_channel_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_channel_peers_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_channel_peers_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_peers")),
        sa.UniqueConstraint(
            "account_id", "peer_id", "workspace_id", name="uq_channel_peers_account_peer_workspace"
        ),
    )
    op.create_index(
        "ix_channel_peers_account_inbound", "channel_peers", ["account_id", "last_inbound_at"], unique=False
    )
    op.create_table(
        "channel_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("peer_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("ilink_message_id", sa.String(length=80), nullable=True),
        sa.Column("client_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("body_excerpt", sa.String(length=500), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name=op.f("ck_channel_messages_direction")),
        sa.CheckConstraint(
            "status IN ('received', 'skipped', 'sent', 'failed')", name=op.f("ck_channel_messages_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["channel_accounts.id"],
            name=op.f("fk_channel_messages_account_id_channel_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_channel_messages_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_messages")),
        sa.UniqueConstraint("account_id", "ilink_message_id", name="uq_channel_messages_account_message"),
    )
    op.create_index("ix_channel_messages_run_id", "channel_messages", ["run_id"], unique=False)
    op.create_table(
        "channel_approval_codes",
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'used')", name=op.f("ck_channel_approval_codes_status")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["channel_accounts.id"],
            name=op.f("fk_channel_approval_codes_account_id_channel_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name=op.f("fk_channel_approval_codes_approval_id_approvals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_channel_approval_codes_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_channel_approval_codes_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_channel_approval_codes")),
    )
    op.create_index("ix_channel_approval_codes_account_id", "channel_approval_codes", ["account_id"], unique=False)
    op.execute("UPDATE system_metadata SET value = '16' WHERE key = 'schema_version'")


def downgrade() -> None:
    """删除微信渠道表。"""
    op.drop_index("ix_channel_approval_codes_account_id", table_name="channel_approval_codes")
    op.drop_table("channel_approval_codes")
    op.drop_index("ix_channel_messages_run_id", table_name="channel_messages")
    op.drop_table("channel_messages")
    op.drop_index("ix_channel_peers_account_inbound", table_name="channel_peers")
    op.drop_table("channel_peers")
    op.drop_index("ix_channel_bind_sessions_user_id", table_name="channel_bind_sessions")
    op.drop_table("channel_bind_sessions")
    op.drop_index("uq_channel_accounts_user_open", table_name="channel_accounts")
    op.drop_index("ix_channel_accounts_status", table_name="channel_accounts")
    op.drop_table("channel_accounts")
    op.execute("UPDATE system_metadata SET value = '15' WHERE key = 'schema_version'")
