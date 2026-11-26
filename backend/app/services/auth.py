"""服务端会话、工作空间与资源鉴权。"""

import asyncio
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, String, cast, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.core.security import csrf_token, digest_token, hash_password, verify_password
from app.models.conversation import Conversation, Run
from app.models.identity import AuthSession, LoginLimit, User, Workspace, WorkspaceMembership
from app.models.system import FIRST_ADMIN_KEY, SystemMetadata
from app.schemas.auth import AuthContext, PasswordChange, ResourceScope, SessionView, UserView, WorkspaceView
from app.services.accounts import (
    MAX_MEMBER_PASSWORD_LENGTH,
    MIN_MEMBER_PASSWORD_LENGTH,
    member_initial_password,
    replace_password_hash,
)


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

    def is_system_admin(self, context: AuthContext) -> bool:
        """优先服从部署配置，默认使用网页首次初始化写入的账号 ID。"""
        username = self.settings.system_admin_username.strip().lower()
        return context.username == username if username else context.first_admin

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
                    select(
                        AuthSession,
                        User,
                        select(SystemMetadata.key)
                        .where(SystemMetadata.key == FIRST_ADMIN_KEY, SystemMetadata.value == cast(User.id, String))
                        .exists(),
                    )
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
            stored, user, first_admin = row
            return AuthContext(
                user.id,
                stored.id,
                user.username,
                user.display_name,
                stored.expires_at,
                first_admin,
                user.must_change_password,
            )

    async def view(self, context: AuthContext, token: str) -> SessionView:
        """只向当前用户返回自己的成员空间与 CSRF 值。

        Args:
            context: 已验证会话。
            token: 原始 Cookie 值。

        Returns:
            安全会话信息。
        """
        user = UserView(id=context.user_id, username=context.username, display_name=context.display_name)
        if context.must_change_password:
            return SessionView(
                user=user,
                workspaces=[],
                expires_at=context.expires_at,
                csrf_token=csrf_token(token),
                must_change_password=True,
            )
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
                user=user,
                system_admin=self.is_system_admin(context),
                workspaces=[
                    WorkspaceView(
                        id=workspace.id, slug=workspace.slug, name=workspace.name, status=workspace.status, role=role
                    )
                    for workspace, role in rows
                ],
                expires_at=context.expires_at,
                csrf_token=csrf_token(token),
            )

    async def change_password(self, context: AuthContext, body: PasswordChange) -> None:
        """核验当前密码，写入时再次确认会话未被其他改密或重置撤销。"""
        current = body.current_password.get_secret_value()
        new = body.new_password.get_secret_value()
        if not (MIN_MEMBER_PASSWORD_LENGTH <= len(new) <= MAX_MEMBER_PASSWORD_LENGTH and new.strip()):
            raise ApplicationError("PASSWORD_INVALID", "新密码需为 8 至 128 个字符，且不能全为空白", 422)
        async with self.factory() as session:
            old_hash = await session.scalar(select(User.password_hash).where(User.id == context.user_id))
        if not await asyncio.to_thread(verify_password, current, old_hash):
            raise ApplicationError("CURRENT_PASSWORD_INVALID", "当前密码不正确", 422)
        initial = await asyncio.to_thread(member_initial_password, self.settings)
        if new == current or new == initial:
            raise ApplicationError("PASSWORD_REUSED", "新密码不能与当前密码或统一初始密码相同", 422)
        encoded = await asyncio.to_thread(hash_password, new)
        async with self.factory.begin() as session:
            user = await session.scalar(
                select(User)
                .join(AuthSession)
                .where(
                    User.id == context.user_id,
                    User.status == "active",
                    User.password_hash == old_hash,
                    AuthSession.id == context.session_id,
                    AuthSession.session_version == User.session_version,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > datetime.now(UTC),
                )
                .with_for_update(of=(User, AuthSession))
            )
            if user is None:
                raise ApplicationError("AUTH_REQUIRED", "登录已失效，请重新登录", 401)
            replace_password_hash(user, encoded)

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
