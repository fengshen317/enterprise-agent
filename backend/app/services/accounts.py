"""本机账号管理与显式历史认领，不在迁移中写默认凭证。"""

import asyncio
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.core.security import hash_password, normalize_username
from app.models.conversation import Conversation, Run
from app.models.identity import User, Workspace, WorkspaceMembership
from app.models.system import FIRST_ADMIN_KEY, SystemMetadata

ACCOUNT_LOCK = 782341913
MIN_PASSWORD_LENGTH = 4


def _validate_new_password(password: str) -> None:
    """在首次创建和重置时执行同一条密码规则。

    Args:
        password: 用户输入的新密码，保留原始字符。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ApplicationError("PASSWORD_TOO_SHORT", f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符", 422)


async def initial_setup_available(session: AsyncSession) -> bool:
    """网页初始化只对尚无账号和空间的部署开放。"""
    if await session.get(SystemMetadata, FIRST_ADMIN_KEY) is not None:
        return False
    return (
        await session.scalar(select(User.id).limit(1)) is None
        and await session.scalar(select(Workspace.id).limit(1)) is None
    )


class AccountManager:
    """在管理命令和一次性部署流程中原子创建账号。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """保存独立会话工厂。

        Args:
            factory: 应用数据库会话。
        """
        self.factory = factory

    async def create(
        self,
        username: str,
        password: str,
        display_name: str,
        slug: str = "default",
        workspace_name: str = "默认工作空间",
        role: str = "member",
        initialize: bool = False,
        claim_legacy: bool = False,
        *,
        first_setup: bool = False,
    ) -> tuple[UUID, UUID, int]:
        """原子创建账号与成员，初始化时同时创建空间。

        Args:
            username: 登录名。
            password: 本机输入的新密码。
            display_name: 中文显示名。
            slug: 空间唯一标识。
            workspace_name: 新空间显示名。
            role: 新成员角色。
            initialize: 是否创建新空间与管理员。
            claim_legacy: 是否显式认领无归属对话。
            first_setup: 是否强制空库并记录部署级管理员。

        Returns:
            用户标识、空间标识及认领数量。
        """
        username = normalize_username(username)
        if (
            re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", slug) is None
            or not display_name.strip()
            or len(display_name) > 100
            or not workspace_name.strip()
            or len(workspace_name) > 100
            or role not in ("admin", "member")
        ):
            raise ApplicationError("INVALID_ACCOUNT_INPUT", "账号显示名、空间或角色不合法", 422)
        if claim_legacy and not initialize:
            raise ApplicationError("INVALID_ACCOUNT_INPUT", "添加普通账号时不能认领历史数据", 422)
        if first_setup and (not initialize or claim_legacy):
            raise ApplicationError("INVALID_ACCOUNT_INPUT", "网页初始化必须创建管理员和空间", 422)
        if initialize:
            _validate_new_password(password)
        encoded = await asyncio.to_thread(hash_password, password)
        async with self.factory.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ACCOUNT_LOCK})
            if first_setup and not await initial_setup_available(session):
                raise ApplicationError("INITIAL_SETUP_CLOSED", "首次初始化已关闭，请使用现有账号登录", 409)
            if await session.scalar(select(User.id).where(User.username == username)) is not None:
                raise ApplicationError("ACCOUNT_EXISTS", "账号已存在，未更改原密码；重置请使用 reset-password", 409)
            workspace = await session.scalar(select(Workspace).where(Workspace.slug == slug).with_for_update())
            if initialize and workspace is not None:
                raise ApplicationError("WORKSPACE_EXISTS", "工作空间已存在，未创建账号", 409)
            if not initialize and (workspace is None or workspace.status != "active"):
                raise ApplicationError("WORKSPACE_NOT_FOUND", "活跃工作空间不存在，请先初始化", 404)
            user = User(username=username, display_name=display_name.strip(), password_hash=encoded)
            session.add(user)
            await session.flush()
            if initialize:
                workspace = Workspace(slug=slug, name=workspace_name.strip(), created_by_user_id=user.id)
                session.add(workspace)
                await session.flush()
            assert workspace is not None
            session.add(
                WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="admin" if initialize else role)
            )
            if first_setup:
                session.add(SystemMetadata(key=FIRST_ADMIN_KEY, value=str(user.id)))
            await session.flush()
            count = await self._claim(session, user.id, workspace.id) if claim_legacy else 0
            return user.id, workspace.id, count

    async def _claim(self, session: AsyncSession, user_id: UUID, workspace_id: UUID) -> int:
        """仅认领两个归属字段都为空的数据，不覆盖已拥有记录。

        Args:
            session: 管理事务。
            user_id: 新归属用户。
            workspace_id: 新归属空间。

        Returns:
            本次认领数量。
        """
        active = await session.scalar(
            select(Run.id)
            .join(Conversation)
            .where(Conversation.workspace_id.is_(None), Run.status.in_(("pending", "running")))
            .limit(1)
        )
        if active is not None:
            raise ApplicationError("LEGACY_RUNS_ACTIVE", "仍有未结束的旧运行，请先停止或重启后端收敛状态", 409)
        ids = await session.scalars(
            update(Conversation)
            .where(Conversation.workspace_id.is_(None), Conversation.created_by_user_id.is_(None))
            .values(workspace_id=workspace_id, created_by_user_id=user_id)
            .returning(Conversation.id)
        )
        return len(list(ids))

    async def claim(self, username: str, slug: str) -> int:
        """管理员目标必须显式选定，重复执行只处理剩余无归属记录。

        Args:
            username: 接收历史的管理员。
            slug: 接收空间。

        Returns:
            认领数量。
        """
        username = normalize_username(username)
        async with self.factory.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ACCOUNT_LOCK})
            row = (
                await session.execute(
                    select(User.id, Workspace.id)
                    .select_from(WorkspaceMembership)
                    .join(User, User.id == WorkspaceMembership.user_id)
                    .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
                    .where(
                        User.username == username,
                        Workspace.slug == slug,
                        User.status == "active",
                        Workspace.status == "active",
                        WorkspaceMembership.status == "active",
                        WorkspaceMembership.role == "admin",
                    )
                )
            ).one_or_none()
            if row is None:
                raise ApplicationError("CLAIM_TARGET_INVALID", "认领目标必须是活跃空间中的有效管理员", 409)
            return await self._claim(session, row[0], row[1])

    async def reset_password(self, username: str, password: str) -> None:
        """重置时递增会话版本，使此前登录立即失效。

        Args:
            username: 目标账号。
            password: 本机输入的新密码。
        """
        username = normalize_username(username)
        _validate_new_password(password)
        encoded = await asyncio.to_thread(hash_password, password)
        async with self.factory.begin() as session:
            user = await session.scalar(select(User).where(User.username == username).with_for_update())
            if user is None:
                raise ApplicationError("ACCOUNT_NOT_FOUND", "账号不存在", 404)
            user.password_hash = encoded
            user.session_version += 1
            user.password_changed_at = datetime.now(UTC)
            user.updated_at = datetime.now(UTC)
