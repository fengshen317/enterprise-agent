"""集成配置服务：DB 值优先于 .env，写入即生效。"""

import logging
from uuid import UUID

from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.config import IntegrationConfig

logger = logging.getLogger(__name__)

# 可管理的配置键：键名 → (是否密钥, 描述)
MANAGED_KEYS: dict[str, tuple[bool, str]] = {
    "deepseek_api_key": (True, "DeepSeek API Key"),
    "deepseek_model": (False, "模型名称"),
    "model_timeout_seconds": (False, "模型超时（秒）"),
    "model_max_tokens": (False, "最大 Token 数"),
    "tavily_api_key": (True, "Tavily API Key"),
    "web_search_timeout_seconds": (False, "搜索超时（秒）"),
    "web_search_max_results": (False, "搜索返回条数"),
}


def mask_secret(value: str) -> str:
    """密钥掩码：保留前 3 后 3，中间替换为 ***。

    Args:
        value: 原始密钥。

    Returns:
        掩码后的字符串。
    """
    if len(value) <= 6:
        return "***"
    return value[:3] + "***" + value[-3:]


class ConfigItem(BaseModel):
    """前端可见的配置项。"""

    key: str
    value_masked: str
    is_secret: bool
    description: str
    updated_at: str | None = None
    updated_by: str | None = None


class ConfigService:
    """管理集成配置的读写；DB 值优先，.env 为默认值。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        """保存会话工厂和 .env 默认值。

        Args:
            factory: 数据库会话工厂。
            settings: .env 默认配置。
        """
        self._factory = factory
        self._settings = settings
        self._cache: dict[str, str] = {}

    async def load(self) -> None:
        """从数据库加载覆盖值到缓存，并同步到 Settings。"""
        async with self._factory() as session:
            rows = (await session.execute(select(IntegrationConfig))).scalars().all()
            self._cache = {row.key: row.value for row in rows}
        # 启动时把 DB 覆盖值写入 Settings，保证所有读取点一致。
        for key, value in self._cache.items():
            try:
                is_secret = key in MANAGED_KEYS and MANAGED_KEYS[key][0]
                if is_secret:
                    setattr(self._settings, key, SecretStr(value))
                elif key in ("model_timeout_seconds", "web_search_timeout_seconds"):
                    setattr(self._settings, key, float(value))
                elif key in ("model_max_tokens", "web_search_max_results"):
                    setattr(self._settings, key, int(value))
                else:
                    setattr(self._settings, key, value)
            except Exception:
                logger.warning("config_load_apply_failed", extra={"key": key})
        logger.info("config_loaded", extra={"count": len(self._cache)})

    def get(self, key: str) -> str:
        """获取配置值；DB 值优先，否则返回 .env 默认值。

        Args:
            key: 配置键名。

        Returns:
            当前生效的值。
        """
        if key in self._cache:
            return self._cache[key]
        return str(getattr(self._settings, key, ""))

    def get_secret(self, key: str) -> SecretStr:
        """获取密钥配置值。

        Args:
            key: 配置键名。

        Returns:
            SecretStr 包装的值。
        """
        return SecretStr(self.get(key))

    def get_int(self, key: str) -> int:
        """获取整数配置值。

        Args:
            key: 配置键名。

        Returns:
            整数值，解析失败返回默认值。
        """
        value = self.get(key)
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return int(getattr(self._settings, key, 0))

    def get_float(self, key: str) -> float:
        """获取浮点配置值。

        Args:
            key: 配置键名。

        Returns:
            浮点值，解析失败返回默认值。
        """
        value = self.get(key)
        try:
            return float(value)
        except (ValueError, TypeError):
            return float(getattr(self._settings, key, 0))

    async def update(self, key: str, value: str, user_id: UUID | None) -> None:
        """写入配置值并更新缓存与 Settings，立即生效。

        Args:
            key: 配置键名。
            value: 新值。
            user_id: 修改人。
        """
        async with self._factory.begin() as session:
            existing = await session.get(IntegrationConfig, key)
            if existing is None:
                session.add(IntegrationConfig(key=key, value=value, updated_by=user_id))
            else:
                existing.value = value
                existing.updated_by = user_id
                from datetime import UTC, datetime

                existing.updated_at = datetime.now(UTC)
        self._cache[key] = value
        # 直接更新共享 Settings 对象，所有读取点立即生效。
        is_secret = key in MANAGED_KEYS and MANAGED_KEYS[key][0]
        try:
            if is_secret:
                setattr(self._settings, key, SecretStr(value))
            elif key in ("model_timeout_seconds", "web_search_timeout_seconds"):
                setattr(self._settings, key, float(value))
            elif key in ("model_max_tokens", "web_search_max_results"):
                setattr(self._settings, key, int(value))
            else:
                setattr(self._settings, key, value)
        except Exception:
            logger.warning("config_settings_update_failed", extra={"key": key})
        logger.info("config_updated", extra={"key": key})

    async def list_configs(self) -> list[ConfigItem]:
        """列出所有可管理的配置项（密钥掩码）。

        Returns:
            配置项列表。
        """
        async with self._factory() as session:
            rows = {row.key: row for row in (await session.execute(select(IntegrationConfig))).scalars().all()}
        items: list[ConfigItem] = []
        for key, (is_secret, description) in MANAGED_KEYS.items():
            row = rows.get(key)
            raw = row.value if row else self.get(key)
            masked = mask_secret(raw) if is_secret else raw
            items.append(
                ConfigItem(
                    key=key,
                    value_masked=masked,
                    is_secret=is_secret,
                    description=description,
                    updated_at=row.updated_at.isoformat() if row else None,
                    updated_by=str(row.updated_by) if row and row.updated_by else None,
                )
            )
        return items
