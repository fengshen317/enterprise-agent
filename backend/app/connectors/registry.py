"""连接器实例注册表；启动构建、运行查找、热更新。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capabilities.registry import ToolRegistry
from app.connectors.base import Connector, ConnectorStatusInfo, ConnectorToolDef, ToolPolicyOverride

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.models.connector import Connector as ConnectorRow

logger = logging.getLogger(__name__)


class ConnectorDescriptor:
    """连接器列表视图：供 API / 前端展示。"""

    def __init__(
        self,
        *,
        key: str,
        kind: str,
        name: str,
        description: str,
        icon: str,
        enabled: bool,
        status: str,
        tool_count: int,
        last_error: str | None = None,
    ) -> None:
        """保存连接器展示信息。

        Args:
            key: 连接器标识。
            kind: 连接器类型。
            name: 展示名称。
            description: 描述。
            icon: 图标标识。
            enabled: 是否启用。
            status: 连接状态。
            tool_count: 发现的工具数。
            last_error: 最近错误信息。
        """
        self.key = key
        self.kind = kind
        self.name = name
        self.description = description
        self.icon = icon
        self.enabled = enabled
        self.status = status
        self.tool_count = tool_count
        self.last_error = last_error


class ConnectorRegistry:
    """连接器实例注册表；启动时构建，运行时查找。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tool_registry: ToolRegistry,
        settings: Settings,
    ) -> None:
        """保存依赖。

        Args:
            factory: 数据库会话工厂。
            tool_registry: 工具注册表（连接器工具注入点）。
            settings: 应用配置。
        """
        self._factory = factory
        self._tool_registry = tool_registry
        self._settings = settings
        self._instances: dict[str, Connector] = {}
        self._descriptors: dict[str, ConnectorDescriptor] = {}

    async def load(self) -> None:
        """启动时读 connectors 表，对 enabled 实例连接并发现工具。

        失败标 error 但不阻断启动。
        """
        from app.models.connector import Connector as ConnectorRow

        try:
            async with self._factory() as session:
                rows = (await session.execute(select(ConnectorRow))).scalars().all()
        except Exception:
            logger.warning("connector_load_failed", extra={"reason": "DB 不可用或表未创建"})
            return

        for row in rows:
            if not row.enabled:
                self._descriptors[row.key] = ConnectorDescriptor(
                    key=row.key,
                    kind=row.kind,
                    name=row.name,
                    description=row.description,
                    icon=row.icon,
                    enabled=False,
                    status="disabled",
                    tool_count=len(row.discovered_tools or []),
                )
                continue
            try:
                connector = self._build_instance(row)
                if connector is None:
                    continue
                self._instances[row.key] = connector
                # connect 和 list_tools 分开：connect 失败不影响工具发现。
                status_info = ConnectorStatusInfo(status="error", message="连接未测试")
                try:
                    status_info = await connector.connect()
                except Exception as exc:
                    status_info = ConnectorStatusInfo(status="error", message=str(exc)[:200])

                defs: list[ConnectorToolDef] = []
                try:
                    defs = await connector.list_tools()
                except Exception:
                    defs = []

                try:
                    specs = connector.tool_specs(defs, self._tool_policies(row.config))
                    self._tool_registry.replace_group(row.key, specs)
                except Exception:
                    self._tool_registry.unregister(row.key)

                self._descriptors[row.key] = ConnectorDescriptor(
                    key=row.key,
                    kind=row.kind,
                    name=row.name,
                    description=row.description,
                    icon=row.icon,
                    enabled=True,
                    status=status_info.status,
                    tool_count=len(defs),
                    last_error=status_info.message if status_info.status != "connected" else None,
                )
                logger.info("connector_loaded", extra={"key": row.key, "tools": len(defs)})
            except Exception as exc:
                self._descriptors[row.key] = ConnectorDescriptor(
                    key=row.key,
                    kind=row.kind,
                    name=row.name,
                    description=row.description,
                    icon=row.icon,
                    enabled=True,
                    status="error",
                    tool_count=len(row.discovered_tools or []),
                    last_error=str(exc)[:200],
                )
                logger.warning("connector_load_error", extra={"key": row.key, "error": str(exc)[:100]})

    def resolve(self, key: str) -> Connector | None:
        """按标识查找连接器实例。

        Args:
            key: 连接器标识。

        Returns:
            连接器实例或空。
        """
        return self._instances.get(key)

    def catalog(self) -> list[ConnectorDescriptor]:
        """返回连接器列表视图。

        Returns:
            连接器描述列表。
        """
        return list(self._descriptors.values())

    def descriptor(self, key: str) -> ConnectorDescriptor | None:
        """按标识查找连接器描述。

        Args:
            key: 连接器标识。

        Returns:
            连接器描述或空。
        """
        return self._descriptors.get(key)

    async def refresh(self, key: str) -> None:
        """配置热更新后重建单个连接器。

        Args:
            key: 连接器标识。
        """
        old = self._instances.pop(key, None)
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass

        try:
            from app.models.connector import Connector as ConnectorRow

            async with self._factory() as session:
                row = (
                    await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))
                ).scalar_one_or_none()
        except Exception:
            return
        if row is None:
            self._tool_registry.unregister(key)
            self._descriptors.pop(key, None)
            return
        if not row.enabled:
            # disabled 状态保留 descriptor，只是不注册工具。
            self._tool_registry.unregister(key)
            self._descriptors[key] = ConnectorDescriptor(
                key=row.key,
                kind=row.kind,
                name=row.name,
                description=row.description,
                icon=row.icon,
                enabled=False,
                status="disabled",
                tool_count=len(row.discovered_tools or []),
            )
            return

        connector = self._build_instance(row)
        if connector is None:
            return
        self._instances[key] = connector
        # connect 和 list_tools 分开：connect 失败不影响工具发现。
        status_info = ConnectorStatusInfo(status="error", message="连接未测试")
        try:
            status_info = await connector.connect()
        except Exception as exc:
            status_info = ConnectorStatusInfo(status="error", message=str(exc)[:200])

        defs: list[ConnectorToolDef] = []
        try:
            defs = await connector.list_tools()
        except Exception:
            defs = []

        try:
            specs = connector.tool_specs(defs, self._tool_policies(row.config))
            self._tool_registry.replace_group(key, specs)
        except Exception:
            self._tool_registry.unregister(key)

        self._descriptors[key] = ConnectorDescriptor(
            key=key,
            kind=row.kind,
            name=row.name,
            description=row.description,
            icon=row.icon,
            enabled=True,
            status=status_info.status,
            tool_count=len(defs),
            last_error=status_info.message if status_info.status != "connected" else None,
        )

    async def close(self) -> None:
        """释放全部连接器资源。"""
        for connector in self._instances.values():
            try:
                await connector.aclose()
            except Exception:
                pass
        self._instances.clear()

    def _build_instance(self, row: ConnectorRow) -> Connector | None:
        """按 kind 构造连接器实例。

        Args:
            row: connectors 表行。

        Returns:
            连接器实例；kind 未知返回空。
        """
        if row.kind == "api":
            from app.connectors.api import ApiConnector

            return cast(Connector, ApiConnector(key=row.key, config=row.config, settings=self._settings))
        if row.kind == "cli":
            from app.connectors.cli import CliConnector

            return cast(Connector, CliConnector(key=row.key, config=row.config, settings=self._settings))
        if row.kind == "mcp":
            from app.connectors.mcp import McpConnector

            return cast(Connector, McpConnector(key=row.key, config=row.config, settings=self._settings))
        logger.warning("connector_unknown_kind", extra={"key": row.key, "kind": row.kind})
        return None

    def _tool_policies(self, config: dict[str, object]) -> dict[str, ToolPolicyOverride]:
        """从配置读取工具风险覆盖。

        Args:
            config: 连接器 config JSONB。

        Returns:
            工具名到覆盖策略的映射。
        """
        raw = config.get("tool_policies", {})
        if not isinstance(raw, dict):
            return {}
        policies: dict[str, ToolPolicyOverride] = {}
        for tool_name, override in raw.items():
            if not isinstance(override, dict):
                continue
            risk = override.get("risk", "external")
            approval = override.get("approval", "always")
            if risk in ("read", "write", "delete", "external") and approval in ("never", "always"):
                policies[str(tool_name)] = ToolPolicyOverride(risk=risk, approval=approval)
        return policies
