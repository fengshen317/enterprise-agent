"""将既有聊天附件归入上传者的个人工作空间材料。"""

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str = "0023"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "chat_attachments"
SOURCE_FK = "fk_chat_attachments_conversation_id_conversations"
OWNER_INDEX = "ix_chat_attachments_owner_created"


def upgrade() -> None:
    """保留原件标识与历史引用，材料的保留周期由工作空间归属决定。"""
    op.add_column(TABLE, sa.Column("workspace_id", sa.Uuid()))
    op.add_column(TABLE, sa.Column("owner_user_id", sa.Uuid()))
    op.add_column(TABLE, sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.execute("""
        UPDATE chat_attachments AS attachment
        SET workspace_id = conversation.workspace_id, owner_user_id = conversation.created_by_user_id,
            created_at = COALESCE((
                SELECT min(message.created_at) FROM messages AS message
                WHERE message.conversation_id = conversation.id
                  AND message.attachments @> jsonb_build_array(jsonb_build_object('id', attachment.id::text))
            ), conversation.created_at)
        FROM conversations AS conversation WHERE conversation.id = attachment.conversation_id
    """)
    for column in ("workspace_id", "owner_user_id", "created_at"):
        op.alter_column(TABLE, column, nullable=False)
    op.drop_constraint(SOURCE_FK, TABLE, type_="foreignkey")
    op.alter_column(TABLE, "conversation_id", nullable=True)
    op.create_foreign_key(SOURCE_FK, TABLE, "conversations", ["conversation_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(
        "fk_chat_attachments_membership", TABLE, "workspace_memberships",
        ["workspace_id", "owner_user_id"], ["workspace_id", "user_id"], ondelete="CASCADE",
    )
    op.create_index(OWNER_INDEX, TABLE, ["workspace_id", "owner_user_id", "created_at", "id"])
    op.execute("UPDATE system_metadata SET value = '24' WHERE key = 'schema_version'")


def downgrade() -> None:
    """仅在旧版本仍能访问全部引用时回退，避免失去已归档材料。"""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM chat_attachments WHERE conversation_id IS NULL)
               OR EXISTS (
                    SELECT 1 FROM messages AS message
                    JOIN chat_attachments AS attachment
                      ON message.attachments @> jsonb_build_array(jsonb_build_object('id', attachment.id::text))
                    WHERE message.conversation_id != attachment.conversation_id
               ) THEN
                RAISE EXCEPTION '个人材料已独立上传或跨对话引用，无法无损回退到 0023';
            END IF;
        END $$
    """)
    op.drop_index(OWNER_INDEX, table_name=TABLE)
    op.drop_constraint("fk_chat_attachments_membership", TABLE, type_="foreignkey")
    op.drop_constraint(SOURCE_FK, TABLE, type_="foreignkey")
    op.alter_column(TABLE, "conversation_id", nullable=False)
    op.create_foreign_key(SOURCE_FK, TABLE, "conversations", ["conversation_id"], ["id"], ondelete="CASCADE")
    for column in ("created_at", "owner_user_id", "workspace_id"):
        op.drop_column(TABLE, column)
    op.execute("UPDATE system_metadata SET value = '23' WHERE key = 'schema_version'")
