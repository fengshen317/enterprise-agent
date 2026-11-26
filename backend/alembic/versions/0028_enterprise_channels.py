"""增加企业渠道应用、员工绑定码，并把定时推送扩展到多个渠道。

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """企业机器人凭证与员工绑定分开存放，已开启的微信推送迁入渠道列表。"""
    op.create_table(
        "channel_apps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("channel_type", sa.String(length=16), nullable=False),
        sa.Column("external_app_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("credentials_ciphertext", sa.Text(), server_default="", nullable=False),
        sa.Column("public_config", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("channel_type IN ('feishu', 'dingtalk', 'qq')", name=op.f("ck_channel_apps_channel_type")),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_channel_apps_status")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_channel_apps_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_channel_apps_created_by_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_apps")),
    )
    op.create_index(
        "uq_channel_apps_type_active",
        "channel_apps",
        ["channel_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_constraint("ck_channel_accounts_channel_type", "channel_accounts", type_="check")
    op.create_check_constraint(
        "ck_channel_accounts_channel_type",
        "channel_accounts",
        "channel_type IN ('ilink', 'feishu', 'dingtalk', 'qq')",
    )
    op.add_column("channel_accounts", sa.Column("app_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_channel_accounts_app_id_channel_apps"),
        "channel_accounts",
        "channel_apps",
        ["app_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_channel_accounts_platform_user",
        "channel_accounts",
        ["channel_type", "ilink_user_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'unbound' AND ilink_user_id <> ''"),
    )
    op.add_column(
        "channel_peers",
        sa.Column("passive_message_id", sa.String(length=128), server_default="", nullable=False),
    )
    op.add_column(
        "channel_peers",
        sa.Column("passive_reply_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "channel_bind_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'used', 'expired')", name=op.f("ck_channel_bind_codes_status")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_channel_bind_codes_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_channel_bind_codes_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["app_id"], ["channel_apps.id"], name=op.f("fk_channel_bind_codes_app_id_channel_apps"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_bind_codes")),
    )
    op.create_index("ix_channel_bind_codes_user_id", "channel_bind_codes", ["user_id"], unique=False)
    op.create_index(
        "uq_channel_bind_codes_pending",
        "channel_bind_codes",
        ["code"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "channel_app_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("peer_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("body_excerpt", sa.String(length=500), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name=op.f("ck_channel_app_events_direction")),
        sa.CheckConstraint(
            "status IN ('received', 'skipped', 'sent', 'failed')", name=op.f("ck_channel_app_events_status")
        ),
        sa.ForeignKeyConstraint(
            ["app_id"], ["channel_apps.id"], name=op.f("fk_channel_app_events_app_id_channel_apps"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_app_events")),
        sa.UniqueConstraint("app_id", "message_id", name="uq_channel_app_events_message"),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column("push_channels", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
    )
    op.execute("UPDATE scheduled_tasks SET push_channels = '[\"ilink\"]'::json WHERE push_to_wechat IS TRUE")
    op.execute("UPDATE system_metadata SET value = '28' WHERE key = 'schema_version'")


def downgrade() -> None:
    """去掉企业渠道表，并恢复渠道类型只允许微信。"""
    op.drop_column("scheduled_tasks", "push_channels")
    op.drop_table("channel_app_events")
    op.drop_index("uq_channel_bind_codes_pending", table_name="channel_bind_codes")
    op.drop_index("ix_channel_bind_codes_user_id", table_name="channel_bind_codes")
    op.drop_table("channel_bind_codes")
    op.drop_column("channel_peers", "passive_reply_count")
    op.drop_column("channel_peers", "passive_message_id")
    op.drop_index("uq_channel_accounts_platform_user", table_name="channel_accounts")
    op.drop_constraint("fk_channel_accounts_app_id_channel_apps", "channel_accounts", type_="foreignkey")
    op.drop_column("channel_accounts", "app_id")
    op.drop_constraint("ck_channel_accounts_channel_type", "channel_accounts", type_="check")
    op.create_check_constraint("ck_channel_accounts_channel_type", "channel_accounts", "channel_type IN ('ilink')")
    op.drop_index("uq_channel_apps_type_active", table_name="channel_apps")
    op.drop_table("channel_apps")
    op.execute("UPDATE system_metadata SET value = '27' WHERE key = 'schema_version'")
