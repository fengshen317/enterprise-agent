"""标记使用初始密码的成员账号。"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留现有用户的登录状态，仅对新开通和重置的用户启用改密要求。"""
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute("UPDATE system_metadata SET value = '28' WHERE key = 'schema_version'")


def downgrade() -> None:
    """仅在没有过渡凭证时移除改密标记。"""
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM users WHERE must_change_password)")):
        raise RuntimeError("仍有使用初始密码的用户，请完成改密后再回退")
    op.drop_column("users", "must_change_password")
    op.execute("UPDATE system_metadata SET value = '27' WHERE key = 'schema_version'")
