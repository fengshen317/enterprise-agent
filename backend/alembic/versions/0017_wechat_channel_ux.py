"""微信渠道补充欢迎标记、窗口提醒和审批风险。

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """记录欢迎是否已发、本窗口是否已提醒，以及确认码对应的风险。"""
    op.add_column(
        "channel_accounts",
        sa.Column("welcome_pending", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "channel_peers",
        sa.Column("reminder_for_inbound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_approval_codes",
        sa.Column("risk_level", sa.String(length=16), server_default="write", nullable=False),
    )
    op.add_column(
        "channel_approval_codes",
        sa.Column("tool_name", sa.String(length=64), server_default="", nullable=False),
    )
    op.create_check_constraint(
        "ck_channel_approval_codes_risk_level",
        "channel_approval_codes",
        "risk_level IN ('write', 'delete', 'external')",
    )
    op.execute("UPDATE system_metadata SET value = '17' WHERE key = 'schema_version'")


def downgrade() -> None:
    """去掉这些展示用字段。"""
    op.drop_constraint("ck_channel_approval_codes_risk_level", "channel_approval_codes", type_="check")
    op.drop_column("channel_approval_codes", "tool_name")
    op.drop_column("channel_approval_codes", "risk_level")
    op.drop_column("channel_peers", "reminder_for_inbound_at")
    op.drop_column("channel_accounts", "welcome_pending")
    op.execute("UPDATE system_metadata SET value = '16' WHERE key = 'schema_version'")
