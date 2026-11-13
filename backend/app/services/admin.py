"""空间管理员业务边界，管理写入与审计在同一事务完成。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.core.security import hash_password, normalize_username
from app.models.admin_audit import AdminAudit
from app.models.identity import User, Workspace, WorkspaceMembership
from app.schemas.admin import AdminAuditView, MemberCreate, MemberPage, MemberUpdate, MemberView
from app.schemas.auth import ResourceScope
from app.services.accounts import ACCOUNT_LOCK


class AdminService:
    """角色只作用于当前空间，不能改变用户在其他空间的权限。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """保存独立会话工厂。

        Args:
            factory: 应用数据库会话。
        """
        self.factory = factory

    async def _authorize(self, session: AsyncSession, scope: ResourceScope, lock: bool = False) -> User:
        """写入先锁空间再重验角色，使并发撤权和末位管理员保护一致。

        Args:
            session: 当前事务。
            scope: 已认证用户与空间。
            lock: 是否串行化空间管理写入。

        Returns:
            当前有效管理员。
        """
        if lock:
            await session.scalar(select(Workspace).where(Workspace.id == scope.workspace_id).with_for_update())
        actor = await session.scalar(
            select(User)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                User.id == scope.user_id,
                User.status == "active",
                Workspace.id == scope.workspace_id,
                Workspace.status == "active",
                WorkspaceMembership.status == "active",
                WorkspaceMembership.role == "admin",
            )
        )
        if actor is None:
            raise ApplicationError("ADMIN_REQUIRED", "仅当前工作空间的管理员可执行此操作", 403)
        return actor

    async def authorize(self, scope: ResourceScope) -> None:
        """在昂贵的密码哈希操作前拒绝非管理员。

        Args:
            scope: 当前资源范围。
        """
        async with self.factory() as session:
            await self._authorize(session, scope)

    @staticmethod
    def _view(user: User, member: WorkspaceMembership) -> MemberView:
        """构建无凭证的成员视图。

        Args:
            user: 账号。
            member: 当前空间成员关系。

        Returns:
            管理公开字段。
        """
        return MemberView.model_validate(
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "user_status": user.status,
                "role": member.role,
                "status": member.status,
                "joined_at": member.created_at,
                "updated_at": member.updated_at,
            }
        )

    async def members(self, scope: ResourceScope, offset: int, limit: int, query: str) -> MemberPage:
        """仅在当前空间内搜索与分页，不提供全局用户目录。

        Args:
            scope: 当前身份与空间。
            offset: 分页偏移。
            limit: 页大小。
            query: 账号或显示名搜索词。

        Returns:
            成员列表与总数。
        """
        async with self.factory() as session:
            await self._authorize(session, scope)
            statement = (
                select(User, WorkspaceMembership)
                .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
                .where(WorkspaceMembership.workspace_id == scope.workspace_id)
            )
            if query.strip():
                statement = statement.where(
                    or_(
                        User.username.icontains(query.strip(), autoescape=True),
                        User.display_name.icontains(query.strip(), autoescape=True),
                    )
                )
            total = await session.scalar(select(func.count()).select_from(statement.subquery()))
            rows = (
                await session.execute(
                    statement.order_by(WorkspaceMembership.created_at, User.id).offset(offset).limit(limit)
                )
            ).all()
            return MemberPage(
                items=[self._view(user, member) for user, member in rows], total=total or 0, offset=offset, limit=limit
            )

    async def create(self, scope: ResourceScope, body: MemberCreate) -> MemberView:
        """原子创建账号、成员和审计，不会覆盖已有账号密码。

        Args:
            scope: 可信管理员范围。
            body: 新账号与固定角色。

        Returns:
            创建后的成员信息。
        """
        await self.authorize(scope)
        username = normalize_username(body.username)
        encoded = await asyncio.to_thread(hash_password, body.password.get_secret_value())
        async with self.factory.begin() as session:
            # 与本机 CLI 使用相同锁顺序，避免重复用户名和交叉创建死锁。
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ACCOUNT_LOCK})
            actor = await self._authorize(session, scope, lock=True)
            if await session.scalar(select(User.id).where(User.username == username)) is not None:
                raise ApplicationError("ACCOUNT_EXISTS", "账号已存在，请使用新的账号名；未修改已有账号", 409)
            user = User(username=username, display_name=body.display_name, password_hash=encoded)
            session.add(user)
            await session.flush()
            member = WorkspaceMembership(workspace_id=scope.workspace_id, user_id=user.id, role=body.role)
            session.add(member)
            await session.flush()
            session.add(
                AdminAudit(
                    workspace_id=scope.workspace_id,
                    actor_user_id=actor.id,
                    actor_username=actor.username,
                    target_user_id=user.id,
                    target_username=user.username,
                    action="create_user",
                    after_role=member.role,
                    after_status=member.status,
                )
            )
            return self._view(user, member)

    async def update(self, scope: ResourceScope, user_id: UUID, body: MemberUpdate) -> MemberView:
        """修改当前空间角色或成员状态，保护最后一名有效管理员。

        Args:
            scope: 当前管理员范围。
            user_id: 目标成员，不接受其他空间的关系。
            body: 角色及状态。

        Returns:
            更新后的成员信息。
        """
        async with self.factory.begin() as session:
            actor = await self._authorize(session, scope, lock=True)
            row = (
                await session.execute(
                    select(User, WorkspaceMembership)
                    .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
                    .where(User.id == user_id, WorkspaceMembership.workspace_id == scope.workspace_id)
                )
            ).one_or_none()
            if row is None:
                raise ApplicationError("MEMBER_NOT_FOUND", "当前空间中不存在该成员", 404)
            user, member = row
            if member.role == body.role and member.status == body.status:
                return self._view(user, member)
            removing_admin = (
                user.status == "active"
                and member.role == "admin"
                and member.status == "active"
                and (body.role != "admin" or body.status != "active")
            )
            if removing_admin:
                count = await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceMembership)
                    .join(User)
                    .where(
                        WorkspaceMembership.workspace_id == scope.workspace_id,
                        WorkspaceMembership.role == "admin",
                        WorkspaceMembership.status == "active",
                        User.status == "active",
                    )
                )
                if (count or 0) <= 1:
                    raise ApplicationError("LAST_ADMIN_REQUIRED", "请至少保留一名有效管理员", 409)
            session.add(
                AdminAudit(
                    workspace_id=scope.workspace_id,
                    actor_user_id=actor.id,
                    actor_username=actor.username,
                    target_user_id=user.id,
                    target_username=user.username,
                    action="update_member",
                    before_role=member.role,
                    before_status=member.status,
                    after_role=body.role,
                    after_status=body.status,
                )
            )
            member.role, member.status, member.updated_at = body.role, body.status, datetime.now(UTC)
            await session.flush()
            return self._view(user, member)

    async def audits(self, scope: ResourceScope, offset: int, limit: int) -> list[AdminAuditView]:
        """管理员只能读取当前空间的管理审计。

        Args:
            scope: 当前管理员范围。
            offset: 分页偏移。
            limit: 页大小。

        Returns:
            最近操作记录。
        """
        async with self.factory() as session:
            await self._authorize(session, scope)
            rows = await session.scalars(
                select(AdminAudit)
                .where(AdminAudit.workspace_id == scope.workspace_id)
                .order_by(AdminAudit.created_at.desc(), AdminAudit.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [AdminAuditView.model_validate(row) for row in rows]
