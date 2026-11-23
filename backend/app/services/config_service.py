"""部署级 API 配置，数据库覆盖值优先于环境变量。"""

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.config import IntegrationConfig

logger = logging.getLogger(__name__)
ENCRYPTED_PREFIX = "fernet:v1:"
LEGACY_NUMERIC_KEYS = {
    "model_timeout_seconds",
    "model_max_tokens",
    "web_search_timeout_seconds",
    "web_search_max_results",
}
MANAGED_KEYS: dict[str, tuple[bool, str]] = {
    "llm_provider": (False, "语言模型供应商"),
    "deepseek_api_key": (True, "DeepSeek API Key"),
    "deepseek_model": (False, "DeepSeek 模型 ID"),
    "llm_base_url": (False, "兼容接口地址"),
    "llm_model": (False, "兼容接口模型 ID"),
    "llm_api_key": (True, "兼容接口 API Key"),
    "llm_vision_model": (False, "兼容接口图文模型 ID"),
    "model_timeout_seconds": (False, "模型超时（秒）"),
    "model_max_tokens": (False, "最大 Token 数"),
    "tavily_api_key": (True, "Tavily API Key"),
    "web_search_timeout_seconds": (False, "搜索超时（秒）"),
    "web_search_max_results": (False, "搜索返回条数"),
}


def cipher_for(path: Path, *, create: bool) -> Fernet:
    """读取部署密钥，首次写入时原子创建并复用同一文件。"""
    try:
        return Fernet(path.read_bytes().strip())
    except FileNotFoundError:
        if not create:
            raise
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(Fernet.generate_key())
            output.flush()
            os.fsync(output.fileno())
            output.close()
            try:
                path.hardlink_to(temporary)
            except FileExistsError:
                # 另一进程创建的密钥优先，防止同一部署出现两个密钥。
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return Fernet(path.read_bytes().strip())


class ConfigItem(BaseModel):
    """管理员可见的非敏感配置和凭据状态。"""

    key: str
    value_masked: str
    is_secret: bool
    description: str
    updated_at: str | None = None
    updated_by: str | None = None


class ConfigService:
    """验证并原子更新部署配置，供后续运行读取。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        """绑定会话工厂和当前进程的运行配置。"""
        self._factory = factory
        self._settings = settings
        self._lock = asyncio.Lock()
        self._loaded = False

    async def _cipher(self, *, create: bool) -> Fernet:
        """文件读写放在线程中，统一屏蔽底层密钥路径。"""
        try:
            return await asyncio.to_thread(cipher_for, self._settings.integration_config_key_file, create=create)
        except (OSError, ValueError) as exc:
            raise ApplicationError("CONFIG_KEY_UNAVAILABLE", "无法读取集成配置加密密钥，请检查部署文件", 503) from exc

    def _apply(self, candidate: Settings, keys: dict[str, str]) -> None:
        """在事务提交后一次性切换运行时配置。"""
        for key in keys:
            setattr(self._settings, key, getattr(candidate, key))

    def _validate(self, values: dict[str, str], *, enforce_profile: bool) -> Settings:
        """验证输入类型和供应商地址，拦截密钥跨服务复用。"""
        try:
            candidate = Settings.model_validate({**self._settings.model_dump(), **values})
            if "llm_base_url" in values or candidate.llm_provider == "openai_compatible":
                url = urlsplit(candidate.llm_base_url)
                _ = url.port
                if (
                    url.scheme not in {"http", "https"}
                    or not url.hostname
                    or url.username is not None
                    or url.password is not None
                    or url.query
                    or url.fragment
                    or any(ch.isspace() for ch in candidate.llm_base_url)
                ):
                    raise ValueError("模型地址不是有效的 API Base URL")
        except (ValidationError, ValueError) as exc:
            raise ApplicationError(
                "CONFIG_VALUE_INVALID", "配置参数无效，请检查模型地址、超时和 Token 范围", 422
            ) from exc
        if not enforce_profile:
            return candidate
        if candidate.llm_provider == "openai_compatible" and not candidate.llm_model:
            raise ApplicationError("CONFIG_MODEL_REQUIRED", "请填写兼容接口的模型 ID", 422)
        if (
            "llm_base_url" in values
            and "llm_api_key" not in values
            and self._settings.llm_api_key.get_secret_value()
            and candidate.llm_base_url.rstrip("/") != self._settings.llm_base_url.rstrip("/")
        ):
            raise ApplicationError("CONFIG_LLM_KEY_REQUIRED", "变更模型地址时，请重新填写或清除对应 API Key", 422)
        return candidate

    async def load(self) -> None:
        """还原全部有效覆盖值，并把历史明文密钥迁移为密文。"""
        async with self._factory.begin() as session:
            rows = (
                await session.scalars(
                    select(IntegrationConfig).where(IntegrationConfig.key.in_(tuple(MANAGED_KEYS))).with_for_update()
                )
            ).all()
            secret_rows = [row for row in rows if MANAGED_KEYS[row.key][0] and row.value]
            cipher = (
                await self._cipher(create=not any(row.value.startswith(ENCRYPTED_PREFIX) for row in secret_rows))
                if secret_rows
                else None
            )
            values = {row.key: row.value for row in rows}
            if cipher is not None:
                for row in secret_rows:
                    if row.value.startswith(ENCRYPTED_PREFIX):
                        try:
                            values[row.key] = cipher.decrypt(row.value.removeprefix(ENCRYPTED_PREFIX).encode()).decode()
                        except InvalidToken as exc:
                            raise ApplicationError("CONFIG_KEY_UNAVAILABLE", "集成配置密钥无法解密", 503) from exc
                    else:
                        row.value = ENCRYPTED_PREFIX + cipher.encrypt(row.value.encode()).decode()
            for key in LEGACY_NUMERIC_KEYS & values.keys():
                try:
                    self._validate({key: values[key]}, enforce_profile=False)
                except ApplicationError:
                    # 旧接口允许保存任意字符串，保留其他值并让管理员修复该项。
                    values.pop(key)
                    logger.warning("config_legacy_value_invalid", extra={"key": key})
            candidate = self._validate(values, enforce_profile=False)
        self._apply(candidate, values)
        self._loaded = True
        logger.info("config_loaded", extra={"count": len(values)})

    async def _ensure_loaded(self) -> None:
        """加载失败时校验旧密钥，避免写入新密文后丢失旧配置。"""
        if not self._loaded:
            await self.load()

    async def update(self, values: dict[str, str], user_id: UUID) -> None:
        """整批校验并保存，避免供应商参数只写入一部分。"""
        if not values.keys() <= MANAGED_KEYS.keys():
            raise ApplicationError("CONFIG_KEY_UNKNOWN", "存在不可管理的配置项", 422)
        values = {key: value.strip() for key, value in values.items()}
        if any(any(ch in value for ch in "\r\n\0") for value in values.values()):
            raise ApplicationError("CONFIG_VALUE_INVALID", "配置值不能包含控制字符", 422)
        async with self._lock:
            await self._ensure_loaded()
            candidate = self._validate(values, enforce_profile=True)
            secrets = {key: value for key, value in values.items() if MANAGED_KEYS[key][0] and value}
            stored = values.copy()
            if secrets:
                cipher = await self._cipher(create=True)
                stored.update(
                    {key: ENCRYPTED_PREFIX + cipher.encrypt(value.encode()).decode() for key, value in secrets.items()}
                )
            async with self._factory.begin() as session:
                updated_at = datetime.now(UTC)
                statement = insert(IntegrationConfig).values(
                    [
                        {"key": key, "value": value, "updated_by": user_id, "updated_at": updated_at}
                        for key, value in stored.items()
                    ]
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[IntegrationConfig.key],
                        set_={
                            "value": statement.excluded.value,
                            "updated_by": statement.excluded.updated_by,
                            "updated_at": statement.excluded.updated_at,
                        },
                    )
                )
            self._apply(candidate, values)
            logger.info("config_updated", extra={"keys": list(values)})

    async def list_configs(self) -> list[ConfigItem]:
        """读取生效配置和更新时间，密钥只显示有无。"""
        async with self._lock, self._factory() as session:
            await self._ensure_loaded()
            rows = {row.key: row for row in (await session.scalars(select(IntegrationConfig))).all()}
            values = {key: getattr(self._settings, key) for key in MANAGED_KEYS}
        items = []
        for key, (is_secret, description) in MANAGED_KEYS.items():
            row = rows.get(key)
            value = values[key]
            masked = str(value)
            if isinstance(value, SecretStr):
                masked = "已配置" if value.get_secret_value().strip() else ""
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
