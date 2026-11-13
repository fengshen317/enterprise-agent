"""增加 Agent 配置、技能版本与工具策略。

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """创建 Agent 工作台元数据，不修改已有问答内容。"""
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("visibility", sa.String(length=16), server_default="private", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("visibility IN ('workspace', 'private')", name=op.f("ck_skills_visibility")),
        sa.CheckConstraint("status IN ('draft', 'published', 'archived')", name=op.f("ck_skills_status")),
        sa.CheckConstraint("current_version > 0", name=op.f("ck_skills_current_version_positive")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_skills_name_nonempty")),
        sa.CheckConstraint("key ~ '^[a-z0-9][a-z0-9_-]{1,63}$'", name=op.f("ck_skills_key_format")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_skills_membership",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
        sa.UniqueConstraint("workspace_id", "key", name="uq_skills_workspace_key"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_skills_id_workspace"),
    )
    op.create_index(
        "ix_skills_workspace_visibility",
        "skills",
        ["workspace_id", "visibility", "updated_at", "id"],
        unique=False,
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "tool_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text('\'{"mode":"all_readable"}\'::jsonb'),
            nullable=False,
        ),
        sa.Column("activation", sa.String(length=16), server_default="always", nullable=False),
        sa.Column("token_budget", sa.Integer(), server_default="4000", nullable=False),
        sa.Column("change_note", sa.String(length=500), server_default="", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name=op.f("ck_skill_versions_version_positive")),
        sa.CheckConstraint("activation IN ('always', 'on_demand')", name=op.f("ck_skill_versions_activation")),
        sa.CheckConstraint("token_budget > 0", name=op.f("ck_skill_versions_token_budget_positive")),
        sa.CheckConstraint("length(content_md) > 0", name=op.f("ck_skill_versions_content_nonempty")),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name="fk_skill_versions_skill_id_skills", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name="fk_skill_versions_created_by_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_versions")),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
    )
    op.create_index(
        "ix_skill_versions_skill_created",
        "skill_versions",
        ["skill_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("icon", sa.String(length=32), server_default="≋", nullable=False),
        sa.Column("instructions", sa.Text(), server_default="", nullable=False),
        sa.Column("visibility", sa.String(length=16), server_default="private", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("model", sa.String(length=200), server_default="", nullable=False),
        sa.Column("allow_planning", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_delegation", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "knowledge_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text('\'{"mode":"all_readable"}\'::jsonb'),
            nullable=False,
        ),
        sa.Column(
            "starter_prompts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("visibility IN ('workspace', 'private')", name=op.f("ck_agent_profiles_visibility")),
        sa.CheckConstraint("status IN ('active', 'archived')", name=op.f("ck_agent_profiles_status")),
        sa.CheckConstraint("version > 0", name=op.f("ck_agent_profiles_version_positive")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_agent_profiles_name_nonempty")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_user_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.user_id"],
            name="fk_agent_profiles_membership",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_profiles")),
        sa.UniqueConstraint("id", "workspace_id", name="uq_agent_profiles_id_workspace"),
    )
    op.create_index(
        "ix_agent_profiles_workspace_visibility",
        "agent_profiles",
        ["workspace_id", "visibility", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_profiles_owner_updated",
        "agent_profiles",
        ["workspace_id", "owner_user_id", "updated_at", "id"],
        unique=False,
    )
    op.create_table(
        "agent_capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_profile_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("capability_key", sa.String(length=64), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=True),
        sa.Column("activation", sa.String(length=16), server_default="always", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('tool', 'skill')", name=op.f("ck_agent_capabilities_kind")),
        sa.CheckConstraint("activation IN ('always', 'on_demand')", name=op.f("ck_agent_capabilities_activation")),
        sa.CheckConstraint(
            "(kind = 'tool' AND skill_version_id IS NULL) OR (kind = 'skill' AND skill_version_id IS NOT NULL)",
            name=op.f("ck_agent_capabilities_target"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            name="fk_agent_capabilities_agent_profile_id_agent_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name="fk_agent_capabilities_skill_version_id_skill_versions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_capabilities")),
        sa.UniqueConstraint("agent_profile_id", "kind", "capability_key", name="uq_agent_capabilities_target"),
    )
    op.create_index(
        "ix_agent_capabilities_agent_kind",
        "agent_capabilities",
        ["agent_profile_id", "kind"],
        unique=False,
    )
    op.create_table(
        "workspace_tool_policies",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("tool_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_tool_policies_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_workspace_tool_policies_updated_by_user_id_users",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "tool_key", name=op.f("pk_workspace_tool_policies")),
    )
    op.create_index(
        "ix_workspace_tool_policies_workspace",
        "workspace_tool_policies",
        ["workspace_id", "enabled"],
        unique=False,
    )
    op.add_column("conversations", sa.Column("agent_profile_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_conversations_agent_profile_id_agent_profiles",
        "conversations",
        "agent_profiles",
        ["agent_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_conversations_agent",
        "conversations",
        ["workspace_id", "agent_profile_id"],
        unique=False,
    )
    op.add_column("runs", sa.Column("agent_profile_id", sa.Uuid(), nullable=True))
    op.add_column(
        "runs",
        sa.Column(
            "agent_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_runs_agent_profile_id_agent_profiles",
        "runs",
        "agent_profiles",
        ["agent_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE system_metadata SET value = '12' WHERE key = 'schema_version'")


def downgrade() -> None:
    """移除 Agent 工作台元数据，不影响历史消息。"""
    op.drop_constraint("fk_runs_agent_profile_id_agent_profiles", "runs", type_="foreignkey")
    op.drop_column("runs", "agent_snapshot")
    op.drop_column("runs", "agent_profile_id")
    op.drop_index("ix_conversations_agent", table_name="conversations")
    op.drop_constraint("fk_conversations_agent_profile_id_agent_profiles", "conversations", type_="foreignkey")
    op.drop_column("conversations", "agent_profile_id")
    op.drop_index("ix_workspace_tool_policies_workspace", table_name="workspace_tool_policies")
    op.drop_table("workspace_tool_policies")
    op.drop_index("ix_agent_capabilities_agent_kind", table_name="agent_capabilities")
    op.drop_table("agent_capabilities")
    op.drop_index("ix_agent_profiles_owner_updated", table_name="agent_profiles")
    op.drop_index("ix_agent_profiles_workspace_visibility", table_name="agent_profiles")
    op.drop_table("agent_profiles")
    op.drop_index("ix_skill_versions_skill_created", table_name="skill_versions")
    op.drop_table("skill_versions")
    op.drop_index("ix_skills_workspace_visibility", table_name="skills")
    op.drop_table("skills")
    op.execute("UPDATE system_metadata SET value = '11' WHERE key = 'schema_version'")
