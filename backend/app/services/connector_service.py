"""连接器服务：配置 CRUD、密钥管理与热更新触发。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.catalog import BUILTIN_CONNECTORS, get_builtin
from app.connectors.registry import ConnectorRegistry
from app.core.errors import ApplicationError
from app.models.config import IntegrationConfig
from app.models.connector import Connector as ConnectorRow
from app.schemas.connectors import (
    ConnectorDetail,
    ConnectorTestResult,
    ConnectorToolView,
    ConnectorView,
)

logger = logging.getLogger(__name__)


class ConnectorService:
    """连接器配置管理；密钥存 integration_configs，非密钥存 connectors.config。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        registry: ConnectorRegistry,
    ) -> None:
        """保存依赖。

        Args:
            factory: 数据库会话工厂。
            registry: 连接器注册表（热更新触发点）。
        """
        self._factory = factory
        self._registry = registry

    async def list_connectors(self) -> list[ConnectorView]:
        """列出全部连接器。

        Returns:
            连接器列表视图。
        """
        descriptors = self._registry.catalog()
        return [
            ConnectorView(
                key=d.key,
                kind=d.kind,
                name=d.name,
                description=d.description,
                icon=d.icon,
                enabled=d.enabled,
                status=d.status,
                tool_count=d.tool_count,
                last_error=d.last_error,
            )
            for d in descriptors
        ]

    async def get_detail(self, key: str) -> ConnectorDetail:
        """获取连接器详情（含工具和配置值）。

        Args:
            key: 连接器标识。

        Returns:
            连接器详情。

        Raises:
            ApplicationError: 连接器不存在。
        """
        desc = self._registry.descriptor(key)
        if desc is None:
            raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)

        connector = self._registry.resolve(key)
        config_fields: list[dict[str, Any]] = []
        tools: list[ConnectorToolView] = []
        config_values: dict[str, str] = {}

        if connector is not None:
            config_fields = [
                {
                    "key": f.key,
                    "label": f.label,
                    "field_type": f.field_type,
                    "required": f.required,
                    "default": f.default,
                    "placeholder": f.placeholder,
                    "is_secret": f.is_secret,
                }
                for f in connector.config_fields()
            ]
            try:
                defs = await connector.list_tools()
                tools = [
                    ConnectorToolView(
                        name=d.name,
                        description=d.description,
                        input_schema=d.input_schema,
                        risk=d.risk,
                        approval=d.approval,
                    )
                    for d in defs
                ]
            except Exception:
                pass

        # connector 不可用（如 disabled）时从内置目录获取配置字段和工具
        if not config_fields:
            builtin = get_builtin(key)
            if builtin is not None:
                config_fields = [dict(f) for f in builtin.config_fields]

        # 读取配置值（密钥掩码）
        async with self._factory() as session:
            row = (
                await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))
            ).scalar_one_or_none()
            if row is not None:
                raw_config = row.config if isinstance(row.config, dict) else {}
                for field in config_fields:
                    if field["is_secret"]:
                        # 从 integration_configs 读取密钥并掩码
                        secret_key = f"connector.{key}.{field['key']}"
                        secret = await session.get(IntegrationConfig, secret_key)
                        if secret is not None:
                            config_values[field["key"]] = _mask(secret.value)
                    else:
                        config_values[field["key"]] = str(raw_config.get(field["key"], field["default"]))

        return ConnectorDetail(
            key=desc.key,
            kind=desc.kind,
            name=desc.name,
            description=desc.description,
            icon=desc.icon,
            enabled=desc.enabled,
            status=desc.status,
            last_error=desc.last_error,
            config_fields=config_fields,
            config_values=config_values,
            tools=tools,
        )

    async def update_config(
        self, key: str, config: dict[str, Any], secrets: dict[str, str]
    ) -> ConnectorDetail:
        """更新连接器配置（非密钥 + 密钥），并触发热更新。

        Args:
            key: 连接器标识。
            config: 非密钥配置。
            secrets: 密钥字段（写入 integration_configs）。

        Returns:
            更新后的连接器详情。

        Raises:
            ApplicationError: 连接器不存在。
        """
        desc = self._registry.descriptor(key)
        if desc is None:
            raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)

        async with self._factory.begin() as session:
            row = (
                await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))
            ).scalar_one_or_none()
            if row is None:
                raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)

            # 合并非密钥配置
            merged = dict(row.config) if isinstance(row.config, dict) else {}
            merged.update(config)
            row.config = merged

            # 写入密钥到 integration_configs
            for field_name, value in secrets.items():
                if not value:
                    continue
                secret_key = f"connector.{key}.{field_name}"
                existing = await session.get(IntegrationConfig, secret_key)
                if existing is None:
                    session.add(IntegrationConfig(key=secret_key, value=value))
                else:
                    existing.value = value

        # 热更新连接器
        await self._registry.refresh(key)
        return await self.get_detail(key)

    async def set_enabled(self, key: str, enabled: bool) -> ConnectorDetail:
        """启停连接器。

        Args:
            key: 连接器标识。
            enabled: 是否启用。

        Returns:
            更新后的连接器详情。
        """
        async with self._factory.begin() as session:
            row = (
                await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))
            ).scalar_one_or_none()
            if row is None:
                raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)
            row.enabled = enabled
            row.status = "disabled" if not enabled else "connected"

        await self._registry.refresh(key)
        return await self.get_detail(key)

    async def test_connection(self, key: str) -> ConnectorTestResult:
        """测试连接并重新发现工具。

        Args:
            key: 连接器标识。

        Returns:
            测试结果。
        """
        await self._registry.refresh(key)
        desc = self._registry.descriptor(key)
        if desc is None:
            raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)
        return ConnectorTestResult(
            status=desc.status,
            message=desc.last_error if desc.last_error else ("连接正常" if desc.status == "connected" else "连接失败"),
            tool_count=desc.tool_count,
        )

    async def authenticate(self, key: str) -> dict[str, Any]:
        """触发 CLI 账号认证（OAuth），返回授权信息。

        对 CLI 连接器执行 auth login --no-browser，返回授权 URL。
        对 API 连接器返回配置提示。

        Args:
            key: 连接器标识。

        Returns:
            认证信息（auth_url / status / message）。

        Raises:
            ApplicationError: 连接器不存在或不支持认证。
        """
        from app.connectors.base import ConnectorError
        from app.connectors.cli import CliConnector

        connector = self._registry.resolve(key)
        if connector is None:
            raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)

        if isinstance(connector, CliConnector):
            cli_path = str(connector._config.get("cli_path", "tmeet"))
            from app.connectors.cli import CliRunner

            runner = CliRunner()
            # 先检查是否已登录
            try:
                status_result = await runner.run([cli_path, "auth", "status"], timeout_seconds=5)
                status_text = status_result.stdout.strip()
                if "Logged in" in status_text:
                    return {"auth_url": "", "output": f"已登录：{status_text[:200]}", "already_logged_in": True}
            except ConnectorError:
                pass  # 未登录或其他错误，继续尝试登录

            try:
                result = await runner.run([cli_path, "auth", "login", "--no-browser"], timeout_seconds=15)
                output = result.stdout + result.stderr
                auth_url = _extract_url(output)
                return {"auth_url": auth_url, "output": output[:500], "already_logged_in": False}
            except ConnectorError as exc:
                return {"auth_url": "", "output": exc.message[:200], "already_logged_in": False}
        else:
            return {"auth_url": "", "output": "API 连接器请在配置中填写 API Key"}

    async def list_builtins(self) -> list[dict[str, Any]]:
        """列出内置连接器模板（含已安装状态）。

        Returns:
            内置模板列表，每个含 key/kind/name/description/icon/installed/config_fields。
        """
        installed_keys: set[str] = set()
        async with self._factory() as session:
            rows = (await session.execute(select(ConnectorRow.key))).scalars().all()
            installed_keys = set(rows)

        result: list[dict[str, Any]] = []
        for item in BUILTIN_CONNECTORS:
            result.append(
                {
                    "key": item.key,
                    "kind": item.kind,
                    "name": item.name,
                    "description": item.description,
                    "icon": item.icon,
                    "installed": item.key in installed_keys,
                    "config_fields": [dict(f) for f in item.config_fields],
                }
            )
        return result

    async def install_builtin(self, key: str) -> ConnectorDetail:
        """从内置模板安装连接器。

        Args:
            key: 内置连接器标识。

        Returns:
            安装后的连接器详情。

        Raises:
            ApplicationError: 模板不存在或已安装。
        """
        builtin = get_builtin(key)
        if builtin is None:
            raise ApplicationError("CONNECTOR_NOT_FOUND", "内置连接器不存在", 404)

        async with self._factory.begin() as session:
            existing = (
                await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))
            ).scalar_one_or_none()
            if existing is not None:
                raise ApplicationError("CONNECTOR_ALREADY_INSTALLED", "连接器已安装", 409)
            row = ConnectorRow(
                key=builtin.key,
                kind=builtin.kind,
                name=builtin.name,
                description=builtin.description,
                icon=builtin.icon,
                enabled=False,
                config=dict(builtin.default_config),
                status="disabled",
            )
            session.add(row)

        await self._registry.refresh(key)
        return await self.get_detail(key)


def _mask(value: str) -> str:
    """密钥掩码：保留前 3 后 3。

    Args:
        value: 原始密钥。

    Returns:
        掩码后的字符串。
    """
    if len(value) <= 6:
        return "***"
    return value[:3] + "***" + value[-3:]


def _extract_url(text: str) -> str:
    """从文本中提取 HTTP(S) URL。

    Args:
        text: 包含 URL 的文本。

    Returns:
        第一个找到的 URL，找不到返回空字符串。
    """
    import re

    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else ""
