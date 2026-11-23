"""为个人应用连接器保存按空间和账号隔离的加密凭证。"""

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """已有连接器继续使用原有配置，个人绑定由成员自行建立。"""
    op.create_table(
        "personal_connector_bindings",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connector_key", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "connector_key"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["connector_key"], ["connectors.key"], ondelete="CASCADE"),
    )
    op.execute("UPDATE system_metadata SET value = '20' WHERE key = 'schema_version'")


def downgrade() -> None:
    """回退移除本版本引入的个人绑定。"""
    op.drop_table("personal_connector_bindings")
    op.execute("UPDATE system_metadata SET value = '19' WHERE key = 'schema_version'")
