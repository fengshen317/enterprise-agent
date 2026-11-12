"""服务端会话、工作空间与资源鉴权。"""

import asyncio
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.core.security import csrf_token, digest_token, verify_password
from app.models.conversation import Conversation, Run
from app.models.identity import AuthSession, LoginLimit, User, Workspace, WorkspaceMembership
from app.schemas.auth import AuthContext, ResourceScope, SessionView, UserView, WorkspaceView


async def require_membership(session: AsyncSession, scope: ResourceScope, write: bool = False) -> None:
    """在当前事务中复核账号及空间成员，不能信任旧的前端角色。

    Args:
        session: 独立事务会话。
        scope: 可信身份和选择的空间。
        write: 是否要求活跃空间。
    """
    status = await session.scalar(
        select(Workspace.status)
        .join(WorkspaceMembership)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(
            Workspace.id == scope.workspace_id,
            WorkspaceMembership.user_id == scope.user_id,
            WorkspaceMembership.status == "active",
            User.status == "active",
        )
    )
    if status is None:
        raise ApplicationError("WORKSPACE_ACCESS_DENIED", "无权访问该工作空间", 403)
    if write and status != "active":
        raise ApplicationError("WORKSPACE_ARCHIVED", "归档空间不能创建对话或发送问题", 409)


def owned_conversations(scope: ResourceScope) -> Select[tuple[Conversation]]:
    """统一对话归属过滤，管理员同样不能读取他人的私有内容。

    Args:
        scope: 已验证的用户和空间。

    Returns:
        私有对话查询。
    """
    return select(Conversation).where(
        Conversation.workspace_id == scope.workspace_id, Conversation.created_by_user_id == scope.user_id
    )


class AuthService:
    """所有身份状态以 PostgreSQL 为准，退出和禁用可立即拒绝新访问。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        """绑定会话工厂和安全配置。

        Args:
            factory: 数据库会话工厂。
            settings: 会话和登录限制。
        """
        self.factory, self.settings = factory, settings

    async def login(self, username: str, password: str, client_host: str) -> str:
        """验证凭证、失败窗口和账号版本后创建随机会话。

        Args:
            username: 用户输入账号。
            password: 用户输入口令。
            client_host: 连接地址，不信任外部转发头。

        Returns:
            仅用于设置 Cookie 的原始令牌。
        """
        username = username.strip().lower()
        key = digest_token(username + "\0" + client_host)
        now = datetime.now(UTC)
        async with self.factory.begin() as session:
            await session.execute(
                insert(LoginLimit).values(key_digest=key, failures=0, window_started_at=now).on_conflict_do_nothing()
            )
            limit = await session.get(LoginLimit, key)
            assert limit is not None
            if (
                (now - limit.window_started_at).total_seconds() < self.settings.auth_login_window_seconds
                and limit.failures >= self.settings.auth_login_max_failures
            ):
                raise ApplicationError("LOGIN_RATE_LIMITED", "登录尝试过多，请稍后重试", 429)
            user = await session.scalar(select(User).where(User.username == username))
            encoded = user.password_hash if user is not None else None
        valid = await asyncio.to_thread(verify_password, password, encoded)
        token = secrets.token_urlsafe(32)
        accepted = False
        async with self.factory.begin() as session:
            limit = (
                await session.scalars(select(LoginLimit).where(LoginLimit.key_digest == key).with_for_update())
            ).one()
            now = datetime.now(UTC)
            if (now - limit.window_started_at).total_seconds() >= self.settings.auth_login_window_seconds:
                limit.failures, limit.window_started_at = 0, now
            if limit.failures >= self.settings.auth_login_max_failures:
                raise ApplicationError("LOGIN_RATE_LIMITED", "登录尝试过多，请稍后重试", 429)
            current = await session.scalar(select(User).where(User.username == username).with_for_update())
            accepted = valid and current is not None and current.status == "active" and current.password_hash == encoded
            if accepted and current is not None:
                limit.failures = 0
                session.add(
                    AuthSession(
                        user_id=current.id,
                        token_digest=digest_token(token),
                        session_version=current.session_version,
                        created_at=now,
                        expires_at=now + timedelta(hours=self.settings.auth_session_hours),
                    )
                )
            else:
                limit.failures += 1
        if not accepted:
            raise ApplicationError("INVALID_CREDENTIALS", "账号或密码错误", 401)
        return token

    async def authenticate(self, token: str) -> AuthContext:
        """逐次校验撤销、过期、账号状态和会话版本。

        Args:
            token: 原始 Cookie 令牌。

        Returns:
            已验证身份。
        """
        if not token or len(token) > 256:
            raise ApplicationError("AUTH_REQUIRED", "请先登录", 401)
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(AuthSession, User)
                    .join(User)
                    .where(
                        AuthSession.token_digest == digest_token(token),
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > datetime.now(UTC),
                        User.status == "active",
                        AuthSession.session_version == User.session_version,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ApplicationError("AUTH_REQUIRED", "登录已失效，请重新登录", 401)
            stored, user = row
            return AuthContext(user.id, stored.id, user.username, user.display_name, stored.expires_at)

    async def view(self, context: AuthContext, token: str) -> SessionView:
        """只向当前用户返回自己的成员空间与 CSRF 值。

        Args:
            context: 已验证会话。
            token: 原始 Cookie 值。

        Returns:
            安全会话信息。
        """
        async with self.factory() as session:
            rows = (
                await session.execute(
                    select(Workspace, WorkspaceMembership.role)
                    .join(WorkspaceMembership)
                    .where(WorkspaceMembership.user_id == context.user_id, WorkspaceMembership.status == "active")
                    .order_by(Workspace.name, Workspace.id)
                )
            ).all()
            return SessionView(
                user=UserView(id=context.user_id, username=context.username, display_name=context.display_name),
                workspaces=[
                    WorkspaceView(
                        id=workspace.id, slug=workspace.slug, name=workspace.name, status=workspace.status, role=role
                    )
                    for workspace, role in rows
                ],
                expires_at=context.expires_at,
                csrf_token=csrf_token(token),
            )

    async def logout(self, token: str) -> None:
        """幂等撤销本会话，不取消仍有资源权限的后台生成。

        Args:
            token: 要撤销的会话。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(AuthSession)
                .where(AuthSession.token_digest == digest_token(token), AuthSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )

    async def scope(self, context: AuthContext, workspace_id: UUID, write: bool = False) -> ResourceScope:
        """把空间选择绑定到经过验证的当前用户。

        Args:
            context: 服务端验证的身份。
            workspace_id: 客户端选择的空间标识。
            write: 是否为创建/生成操作。

        Returns:
            已验证资源范围。
        """
        scope = ResourceScope(context.user_id, workspace_id)
        async with self.factory() as session:
            await require_membership(session, scope, write)
        return scope

    async def conversation(self, scope: ResourceScope, conversation_id: UUID) -> None:
        """对不属于自己的资源统一返回不存在。

        Args:
            scope: 当前范围。
            conversation_id: 被访问的对话。
        """
        async with self.factory() as session:
            await require_membership(session, scope)
            if await session.scalar(owned_conversations(scope).where(Conversation.id == conversation_id)) is None:
                raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)

    async def run(self, scope: ResourceScope, run_id: UUID) -> None:
        """运行、取消和订阅共享相同的归属校验。

        Args:
            scope: 当前范围。
            run_id: 被访问的运行。
        """
        async with self.factory() as session:
            await require_membership(session, scope)
            found = await session.scalar(
                select(Run.id)
                .join(Conversation)
                .where(
                    Run.id == run_id,
                    Conversation.workspace_id == scope.workspace_id,
                    Conversation.created_by_user_id == scope.user_id,
                )
            )
            if found is None:
                raise ApplicationError("RUN_NOT_FOUND", "运行不存在", 404)


def validate_origin(origin: str | None, settings: Settings) -> None:
    """拒绝缺少或不可信来源的状态修改请求。

    Args:
        origin: 浏览器 Origin 头。
        settings: 显式可信来源。
    """
    if origin is None or origin not in settings.auth_trusted_origins:
        raise ApplicationError("ORIGIN_DENIED", "请求来源不受信任", 403)


def validate_csrf(token: str, provided: str | None) -> None:
    """会话绑定 CSRF 值不接受其他会话或任意 Cookie 替代。

    Args:
        token: HttpOnly Cookie 值。
        provided: 客户端 CSRF 请求头。
    """
    if not token or provided is None or not hmac.compare_digest(csrf_token(token), provided):
        raise ApplicationError("CSRF_INVALID", "请求校验失败，请刷新页面后重试", 403)
