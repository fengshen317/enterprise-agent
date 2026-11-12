"""通过独立异步连接执行数据库迁移。"""

import asyncio

from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import Settings
from app.models.admin_audit import AdminAudit
from app.models.agent import AgentCapability, AgentProfile, WorkspaceToolPolicy
from app.models.approval import Approval, ApprovalEvent
from app.models.base import Base
from app.models.config import IntegrationConfig
from app.models.conversation import Conversation, Message, Run
from app.models.identity import AuthSession, LoginLimit, User, Workspace, WorkspaceMembership
from app.models.knowledge import KnowledgeBase, KnowledgeDocument
from app.models.schedule import ScheduledTask
from app.models.skill import Skill, SkillVersion
from app.models.system import SystemMetadata
from app.models.task import Task, TaskAction, TaskActionAttempt, TaskEvent, TaskPlan, TaskRun, TaskStep, TaskStepAttempt
from app.models.tool_call import ToolCall

# 显式加载模型，确保自动比较使用完整元数据。
assert all(
    model.__table__.metadata is Base.metadata
    for model in (
        SystemMetadata,
        AdminAudit,
        Conversation,
        Message,
        Run,
        User,
        Workspace,
        WorkspaceMembership,
        AuthSession,
        LoginLimit,
        ToolCall,
        ScheduledTask,
        KnowledgeBase,
        KnowledgeDocument,
        AgentProfile,
        AgentCapability,
        WorkspaceToolPolicy,
        Skill,
        SkillVersion,
        IntegrationConfig,
        Approval,
        ApprovalEvent,
        Task,
        TaskRun,
        TaskPlan,
        TaskStep,
        TaskStepAttempt,
        TaskAction,
        TaskActionAttempt,
        TaskEvent,
    )
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """生成 SQL，不建立数据库连接。"""
    context.configure(
        url=Settings().sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection: Connection) -> None:
    """在同步适配上下文中运行迁移。

    Args:
        connection: Alembic 使用的数据库连接。
    """
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """使用异步驱动连接，并在完成或失败后释放资源。"""
    engine = create_async_engine(Settings().sqlalchemy_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
