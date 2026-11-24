"""一次性部署口令和首个管理员创建流程。"""

import asyncio
import hmac
import logging
import os
import re
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.core.security import normalize_username
from app.services.accounts import AccountManager, initial_setup_available

logger = logging.getLogger(__name__)
TOKEN_BYTES = 32
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 128


def read_or_create_token(path: Path) -> str:
    """持久化随机口令，确保重启和并发启动读取同一完整文件。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        tmp = Path(temporary.name)
        try:
            temporary.write(secrets.token_urlsafe(TOKEN_BYTES) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            try:
                path.hardlink_to(tmp)
            except FileExistsError:
                # 已有部署生成的口令优先，避免让操作者手中的旧值失效。
                pass
        finally:
            tmp.unlink(missing_ok=True)
    return path.read_text(encoding="utf-8").strip()


class InitialSetupService:
    """只对持有部署口令的操作者开放工作台首次初始化。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        """保存独立会话工厂和部署设置。"""
        self.factory = factory
        self.settings = settings
        self.token = ""
        self.generated_file: Path | None = None

    @property
    def username(self) -> str | None:
        """已指定系统管理员时沿用部署指定的登录名。"""
        return self.settings.system_admin_username.strip().lower() or None

    async def load_token(self) -> None:
        """只在空库启动时加载或生成口令，任何失败都保持入口关闭。"""
        configured = self.settings.initial_setup_token.get_secret_value().strip()
        path = self.settings.initial_setup_token_file
        if not configured and path is None:
            return
        async with self.factory() as session:
            if not await initial_setup_available(session):
                return
        if not configured and path is not None:
            configured = await asyncio.to_thread(read_or_create_token, path)
            self.generated_file = path
        if not (
            MIN_TOKEN_LENGTH <= len(configured) <= MAX_TOKEN_LENGTH and re.fullmatch(r"[A-Za-z0-9_-]+", configured)
        ):
            raise ApplicationError("INITIAL_SETUP_TOKEN_CONFIG", "部署口令格式无效", 503)
        if self.username:
            normalize_username(self.username)
        self.token = configured

    async def available(self) -> bool:
        """返回公开入口状态，口令始终留在部署端。"""
        if not self.token:
            return False
        async with self.factory() as session:
            return await initial_setup_available(session)

    async def create_admin(self, token: str, username: str, display_name: str, password: str) -> str:
        """验证部署口令后，在同一事务内检查空库并创建管理员。"""
        if not self.token:
            raise ApplicationError("INITIAL_SETUP_CLOSED", "首次初始化已关闭，请使用现有账号登录", 409)
        if not hmac.compare_digest(token.strip().encode(), self.token.encode()):
            raise ApplicationError("INITIAL_SETUP_TOKEN_INVALID", "部署口令不正确，请核对服务器上的口令", 403)
        normalized = normalize_username(username)
        if self.username and normalized != self.username:
            raise ApplicationError("INITIAL_SETUP_USERNAME_RESERVED", "请使用部署指定的管理员账号", 422)
        await AccountManager(self.factory).create(normalized, password, display_name, initialize=True, first_setup=True)
        self.token = ""
        if self.generated_file is not None:
            try:
                await asyncio.to_thread(self.generated_file.unlink, missing_ok=True)
            except OSError:
                # 数据库已关闭入口，文件清理失败不改变提交结果。
                logger.warning("initial_setup_token_cleanup_failed")
        return normalized
