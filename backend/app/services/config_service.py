"""部署级 API 配置，数据库覆盖值优先于环境变量。"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import delete, exists, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.core.secret_files import cipher_for
from app.models.config import IntegrationConfig
from app.models.conversation import Run
from app.schemas.model_profiles import ModelProfileDraft, ModelProfileFields, ModelProfileView
from app.services.accounts import MAX_MEMBER_PASSWORD_LENGTH, MIN_MEMBER_PASSWORD_LENGTH

logger = logging.getLogger(__name__)
ENCRYPTED_PREFIX = "fernet:v1:"
MEMBER_INITIAL_PASSWORD_KEY = "member_initial_password"
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
    MEMBER_INITIAL_PASSWORD_KEY: (True, "统一初始密码"),
}
ConfigGroup = Literal["llm", "search"]
SEARCH_KEYS = frozenset({"tavily_api_key", "web_search_timeout_seconds", "web_search_max_results"})
PROFILE_PREFIX = "llm-profile:"
ACTIVE_PROFILE_KEY = "llm-active-profile"
PERSONAL_PROFILE_PREFIX = "personal-model:"
PERSONAL_ACTIVE_PREFIX = "personal-active:"
MODEL_CONNECTION_KEYS = frozenset({
    "llm_provider", "deepseek_model", "deepseek_api_key",
    "llm_base_url", "llm_model", "llm_vision_model", "llm_api_key",
})
SECRET_KEYS = tuple(key for key, (secret, _) in MANAGED_KEYS.items() if secret)


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
            path = self._settings.integration_config_key_file
            try:
                return await asyncio.to_thread(cipher_for, path, create=False)
            except FileNotFoundError:
                if not create:
                    raise
            # 模型单独保存但尚未启用时，也需要保护其密文对应的密钥。
            async with self._factory() as session:
                has_ciphertext = await session.scalar(select(exists().where(
                    or_(
                        IntegrationConfig.key.in_(SECRET_KEYS), IntegrationConfig.key.startswith(PROFILE_PREFIX),
                        IntegrationConfig.key.startswith(PERSONAL_PROFILE_PREFIX),
                    ),
                    IntegrationConfig.value.startswith(ENCRYPTED_PREFIX),
                )))
                has_run_ciphertext = await session.scalar(select(exists().where(Run.model_connection.is_not(None))))
            if has_ciphertext or has_run_ciphertext:
                raise FileNotFoundError("配置加密密钥缺失")
            return await asyncio.to_thread(cipher_for, path, create=True)
        except (OSError, ValueError) as exc:
            raise ApplicationError("CONFIG_KEY_UNAVAILABLE", "无法读取集成配置加密密钥，请检查部署文件", 503) from exc

    def _apply(self, candidate: Settings, keys: dict[str, str]) -> None:
        """在事务提交后一次性切换运行时配置。"""
        for key in keys:
            setattr(self._settings, key, getattr(candidate, key))

    def _validate(self, values: dict[str, str], *, enforce_profile: bool, base: Settings | None = None) -> Settings:
        """验证输入类型和供应商地址，拦截密钥跨服务复用。"""
        current = base or self._settings
        password = values.get(MEMBER_INITIAL_PASSWORD_KEY)
        if password is not None and not (
            MIN_MEMBER_PASSWORD_LENGTH <= len(password) <= MAX_MEMBER_PASSWORD_LENGTH and password.strip()
        ):
            raise ApplicationError("INITIAL_PASSWORD_INVALID", "统一初始密码需为 8 至 128 个字符，且不能全为空白", 422)
        try:
            candidate = Settings.model_validate({**current.model_dump(), **values})
            # 成员密码单独保存时不依赖模型配置是否齐备。
            if values.keys() == {MEMBER_INITIAL_PASSWORD_KEY}:
                return candidate
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

    @staticmethod
    def _normalize(values: dict[str, str], allowed: set[str]) -> dict[str, str]:
        """统一检查保存和测试提交的配置值。"""
        if not values.keys() <= allowed:
            raise ApplicationError("CONFIG_KEY_UNKNOWN", "存在不可管理的配置项", 422)
        if any(ch in values.get(MEMBER_INITIAL_PASSWORD_KEY, "") for ch in "\r\n\0"):
            raise ApplicationError("INITIAL_PASSWORD_INVALID", "统一初始密码需为单行文本", 422)
        # 密码原样保存，其他配置继续沿用原有的首尾空白处理。
        normalized = {
            key: value if key == MEMBER_INITIAL_PASSWORD_KEY else value.strip() for key, value in values.items()
        }
        if any(any(ch in value for ch in "\r\n\0") for value in normalized.values()):
            raise ApplicationError("CONFIG_VALUE_INVALID", "配置值不能包含控制字符", 422)
        return normalized

    async def settings_for_test(self, group: ConfigGroup, values: dict[str, str]) -> Settings:
        """按组构造独立快照，仅在地址匹配时复用已保存的模型密钥。"""
        allowed = (
            set(SEARCH_KEYS) if group == "search" else MANAGED_KEYS.keys() - SEARCH_KEYS - {MEMBER_INITIAL_PASSWORD_KEY}
        )
        values = self._normalize(values, allowed)
        async with self._lock:
            await self._ensure_loaded()
            return self._validate(values, enforce_profile=group == "llm")

    @staticmethod
    async def _write(session: AsyncSession, values: dict[str, str], user_id: UUID) -> None:
        """把配置值及对应的模型标识一起写入同一事务。"""
        updated_at = datetime.now(UTC)
        statement = insert(IntegrationConfig).values([
            {"key": key, "value": value, "updated_by": user_id, "updated_at": updated_at}
            for key, value in values.items()
        ])
        await session.execute(statement.on_conflict_do_update(
            index_elements=[IntegrationConfig.key],
            set_={
                "value": statement.excluded.value,
                "updated_by": statement.excluded.updated_by,
                "updated_at": statement.excluded.updated_at,
            },
        ))

    async def _stored_config(self, values: dict[str, str]) -> dict[str, str]:
        """加密当前配置中的非空凭据，供保存和模型切换复用。"""
        secrets = {key: value for key, value in values.items() if key in SECRET_KEYS and value}
        if not secrets:
            return values.copy()
        cipher = await self._cipher(create=True)
        return {**values, **{
            key: ENCRYPTED_PREFIX + cipher.encrypt(value.encode()).decode() for key, value in secrets.items()
        }}

    async def update(self, values: dict[str, str], user_id: UUID) -> None:
        """整批校验并保存，避免供应商参数只写入一部分。"""
        values = self._normalize(values, set(MANAGED_KEYS))
        async with self._lock:
            await self._ensure_loaded()
            candidate = self._validate(values, enforce_profile=True)
            stored = await self._stored_config(values)
            connection_changed = any(
                getattr(self._settings, key) != getattr(candidate, key) for key in values.keys() & MODEL_CONNECTION_KEYS
            )
            async with self._factory.begin() as session:
                await self._write(session, stored, user_id)
                if connection_changed:
                    await session.execute(delete(IntegrationConfig).where(IntegrationConfig.key == ACTIVE_PROFILE_KEY))
            self._apply(candidate, values)
            logger.info("config_updated", extra={"keys": list(values)})

    @staticmethod
    def _profile_view(profile: ModelProfileDraft, profile_id: UUID, active: bool) -> ModelProfileView:
        """转换为可公开给管理页面的模型选项。"""
        return ModelProfileView(
            **profile.model_dump(exclude={"api_key"}),
            id=profile_id,
            active=active,
            has_api_key=bool(profile.api_key.get_secret_value().strip()),
        )

    @staticmethod
    def _decode_profile(row: IntegrationConfig, cipher: Fernet) -> ModelProfileDraft:
        """读取加密模型记录，并为密钥不匹配提供固定错误提示。"""
        try:
            if not row.value.startswith(ENCRYPTED_PREFIX):
                raise ValueError("模型记录缺少加密标识")
            encoded = row.value.removeprefix(ENCRYPTED_PREFIX).encode()
            return ModelProfileDraft.model_validate_json(cipher.decrypt(encoded))
        except (InvalidToken, ValueError) as exc:
            raise ApplicationError("MODEL_PROFILE_UNAVAILABLE", "已保存模型无法读取，请检查集成配置密钥", 503) from exc

    async def list_model_profiles(self, user_id: UUID | None = None) -> list[ModelProfileView]:
        """列出管理端模型选项和当前选中的默认项。"""
        async with self._lock, self._factory() as session:
            prefix = PERSONAL_PROFILE_PREFIX if user_id else PROFILE_PREFIX
            active_key = PERSONAL_ACTIVE_PREFIX + user_id.hex if user_id else ACTIVE_PROFILE_KEY
            active = await session.get(IntegrationConfig, active_key)
            rows = (await session.scalars(
                select(IntegrationConfig).where(
                    IntegrationConfig.key.startswith(prefix),
                    IntegrationConfig.updated_by == user_id if user_id else true(),
                )
                .order_by(IntegrationConfig.updated_at.desc(), IntegrationConfig.key)
            )).all()
            if not rows:
                return []
            cipher = await self._cipher(create=False)
            return [self._profile_view(
                self._decode_profile(row, cipher),
                UUID(row.key.removeprefix(prefix)),
                active is not None and active.value == row.key.removeprefix(prefix),
            ) for row in rows]

    async def add_model_profile(
        self, draft: ModelProfileDraft, user_id: UUID, *, personal: bool = False, make_default: bool = False,
    ) -> ModelProfileView:
        """加密保存独立模型配置，等待管理员选择默认项。"""
        profile_id = uuid4()
        values = self._normalize(draft.config_values(), set(MODEL_CONNECTION_KEYS))
        async with self._lock:
            await self._ensure_loaded()
            self._validate(values, enforce_profile=True)
            cipher = await self._cipher(create=True)
            payload = {**draft.model_dump(exclude={"api_key"}), "api_key": draft.api_key.get_secret_value().strip()}
            encrypted = ENCRYPTED_PREFIX + cipher.encrypt(json.dumps(payload).encode()).decode()
            async with self._factory.begin() as session:
                prefix = PERSONAL_PROFILE_PREFIX if personal else PROFILE_PREFIX
                stored = {f"{prefix}{profile_id}": encrypted}
                if personal and make_default:
                    stored[PERSONAL_ACTIVE_PREFIX + user_id.hex] = str(profile_id)
                await self._write(session, stored, user_id)
        return self._profile_view(draft, profile_id, active=personal and make_default)

    async def activate_model_profile(self, profile_id: UUID, user_id: UUID, *, personal: bool = False) -> None:
        """原子更新默认模型及其凭据，事务提交后切换后续调用。"""
        async with self._lock:
            await self._ensure_loaded()
            async with self._factory.begin() as session:
                prefix = PERSONAL_PROFILE_PREFIX if personal else PROFILE_PREFIX
                row = await session.get(IntegrationConfig, f"{prefix}{profile_id}")
                if row is None or (personal and row.updated_by != user_id):
                    raise ApplicationError("MODEL_PROFILE_NOT_FOUND", "找不到这个模型配置", 404)
                draft = self._decode_profile(row, await self._cipher(create=False))
                values = self._normalize(draft.config_values(), set(MODEL_CONNECTION_KEYS))
                candidate = self._validate(values, enforce_profile=True)
                if personal:
                    await self._write(session, {PERSONAL_ACTIVE_PREFIX + user_id.hex: str(profile_id)}, user_id)
                    return
                stored = await self._stored_config(values)
                await self._write(session, {**stored, ACTIVE_PROFILE_KEY: str(profile_id)}, user_id)
            self._apply(candidate, values)

    async def follow_shared_model(self, user_id: UUID) -> None:
        """清除当前账号的个人选择，后续请求沿用公共默认模型。"""
        async with self._lock, self._factory.begin() as session:
            await session.execute(delete(IntegrationConfig).where(
                IntegrationConfig.key == PERSONAL_ACTIVE_PREFIX + user_id.hex,
            ))

    async def shared_model_summary(self) -> ModelProfileFields:
        """只构造公共模型的展示字段，凭据始终省略。"""
        async with self._lock:
            await self._ensure_loaded()
            settings = self._settings
            # 展示使用统一字段模型，避免成员接口读取部署凭据。
            return ModelProfileFields(
                name=settings.active_llm_model,
                provider=settings.llm_provider,
                model_id=settings.active_llm_model,
                vision_model_id=settings.llm_vision_model if settings.llm_provider == "openai_compatible" else "",
            )

    async def settings_for_user(self, user_id: UUID, *, base: Settings | None = None) -> Settings:
        """返回当前账号的独立配置快照，个人配置读取失败直接报错。"""
        async with self._lock:
            await self._ensure_loaded()
            async with self._factory() as session:
                active = await session.get(IntegrationConfig, PERSONAL_ACTIVE_PREFIX + user_id.hex)
                if active is None:
                    return (base or self._settings).model_copy(deep=True)
                row = await session.get(IntegrationConfig, PERSONAL_PROFILE_PREFIX + active.value)
                if row is None or row.updated_by != user_id:
                    raise ApplicationError("MODEL_PROFILE_UNAVAILABLE", "个人默认模型不可用，请重新选择", 503)
            draft = self._decode_profile(row, await self._cipher(create=False))
            return self._validate(draft.config_values(), enforce_profile=True, base=base)

    async def seal_model_connection(self, settings: Settings) -> str:
        """加密本轮连接参数，保证切换默认模型不会影响已提交的运行。"""
        values = {key: getattr(settings, key) for key in MODEL_CONNECTION_KEYS}
        payload = {
            key: value.get_secret_value() if isinstance(value, SecretStr) else value for key, value in values.items()
        }
        # 只保留本轮供应商的凭据，避免把部署的另一组密钥复制到个人运行。
        unused_key = "llm_api_key" if settings.llm_provider == "deepseek" else "deepseek_api_key"
        payload[unused_key] = ""
        return ENCRYPTED_PREFIX + (await self._cipher(create=True)).encrypt(json.dumps(payload).encode()).decode()

    async def settings_for_run(self, run_id: UUID, *, base: Settings | None = None) -> Settings:
        """读取内部加密快照，兼容升级前没有快照的运行。"""
        async with self._factory() as session:
            payload = await session.scalar(select(Run.model_connection).where(Run.id == run_id))
        if payload is None:
            return (base or self._settings).model_copy(deep=True)
        try:
            cipher = await self._cipher(create=False)
            values = json.loads(cipher.decrypt(payload.removeprefix(ENCRYPTED_PREFIX).encode()))
            return self._validate(values, enforce_profile=False, base=base)
        except (InvalidToken, ValueError) as exc:
            raise ApplicationError("MODEL_PROFILE_UNAVAILABLE", "本轮模型配置无法读取，请检查集成密钥", 503) from exc

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
