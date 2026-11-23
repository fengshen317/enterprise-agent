"""连接器实例注册表；启动构建、运行查找、热更新。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capabilities.registry import ToolRegistry
from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors.base import (
    Connector,
    ConnectorAuthStatus,
    ConnectorError,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
    health_exception,
    health_failure,
)
from app.connectors.catalog import FUND_MARKET, PERSONAL_CONNECTORS, YANGJIBAO
from app.connectors.yangjibao import BINDING_OWNER_KEY, YangJiBaoConnector
from app.connectors.yangjibao_authorization import YangJiBaoScan
from app.core.errors import ApplicationError

if TYPE_CHECKING:
    from app.connectors.authorization import CliAuthorization
    from app.core.config import Settings
    from app.models.connector import Connector as ConnectorRow
    from app.schemas.auth import ResourceScope

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
        auth_status: str = "unknown",
        last_error_code: str | None = None,
        last_checked_at: datetime | None = None,
        last_success_at: datetime | None = None,
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
            auth_status: 最近一次确认的认证状态。
            last_error_code: 可公开的错误分类。
            last_checked_at: 最近检查时间。
            last_success_at: 最近成功时间。
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
        self.auth_status = auth_status
        self.last_error_code = last_error_code
        self.last_checked_at = last_checked_at
        self.last_success_at = last_success_at

    @property
    def auth_action(self) -> str | None:
        """根据认证和连接结果提供可执行操作。

        Returns:
            授权、配置、重试操作，停用或正常时为空。
        """
        if not self.enabled:
            return None
        if self.auth_status == "required":
            return "authorize" if self.key in {"tmeet", YANGJIBAO.key} else "configure"
        if self.last_error_code in {"not_configured", "permission_denied", "command_failed", "invalid_output"}:
            return "configure"
        return "retry" if self.status != "connected" else None


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
        self._locks: dict[str, asyncio.Lock] = {}
        self._auth_locks: dict[str, asyncio.Lock] = {}
        self._checks: dict[str, asyncio.Task[None]] = {}
        self._monitor: asyncio.Task[None] | None = None
        self._authorizations: dict[str, CliAuthorization] = {}
        self._yangjibao_scan: YangJiBaoScan | None = None

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
                connector = await self._build_instance(row)
                if connector is None:
                    continue
                self._instances[row.key] = connector
                # connect 和 list_tools 分开：connect 失败不影响工具发现。
                status_info = await self._initial_health(connector)

                defs: list[ConnectorToolDef] = []
                try:
                    async with asyncio.timeout(self._settings.connector_check_timeout_seconds):
                        defs = await connector.list_tools()
                except Exception:
                    defs = []

                try:
                    specs = connector.tool_specs(defs, self._tool_policies(row.config))
                    self._tool_registry.replace_group(row.key, [self._observe(spec, connector) for spec in specs])
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
                    auth_status=status_info.auth_status,
                    last_success_at=row.last_success_at,
                )
                await self._apply_health(row.key, connector, status_info)
                logger.info("connector_loaded", extra={"key": row.key, "tools": len(defs)})
            except Exception:
                self._descriptors[row.key] = ConnectorDescriptor(
                    key=row.key,
                    kind=row.kind,
                    name=row.name,
                    description=row.description,
                    icon=row.icon,
                    enabled=True,
                    status="error",
                    tool_count=len(row.discovered_tools or []),
                    last_error="连接器初始化失败，请检查配置",
                )
                logger.warning("connector_load_error", extra={"key": row.key})

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
        async with self._locks.setdefault(key, asyncio.Lock()):
            await self._refresh(key)

    async def _refresh(self, key: str) -> None:
        """在单连接器锁内重建实例。

        Args:
            key: 连接器标识。
        """
        if key == YANGJIBAO.key:
            self._yangjibao_scan = None
        flow = self._authorizations.pop(key, None)
        if flow is not None:
            from app.connectors.authorization import CliAuthorization

            if isinstance(flow, CliAuthorization):
                await flow.close()
        old = self._instances.pop(key, None)
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass

        try:
            from app.models.connector import Connector as ConnectorRow

            async with self._factory() as session:
                row = (await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))).scalar_one_or_none()
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

        connector = await self._build_instance(row)
        if connector is None:
            return
        self._instances[key] = connector
        # connect 和 list_tools 分开：connect 失败不影响工具发现。
        status_info = await self._initial_health(connector)

        defs: list[ConnectorToolDef] = []
        try:
            async with asyncio.timeout(self._settings.connector_check_timeout_seconds):
                defs = await connector.list_tools()
        except Exception:
            defs = []

        try:
            specs = connector.tool_specs(defs, self._tool_policies(row.config))
            self._tool_registry.replace_group(key, [self._observe(spec, connector) for spec in specs])
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
            auth_status=status_info.auth_status,
            last_success_at=row.last_success_at,
        )
        await self._apply_health(key, connector, status_info)

    async def _initial_health(self, connector: Connector) -> ConnectorStatusInfo:
        """限制初始化连接和健康检查的总等待时间。

        Args:
            connector: 新建实例。

        Returns:
            安全的首次检查结果。
        """
        try:
            async with asyncio.timeout(self._settings.connector_check_timeout_seconds):
                info = await connector.connect()
                if connector.kind != "cli" and info.status == "connected":
                    info = await connector.check_health()
                return info
        except Exception as exc:
            return health_exception(exc)

    def start(self) -> None:
        """启动进程级监控，检查之间等待配置的间隔。"""
        if self._settings.connector_monitor_enabled and self._monitor is None:
            self._monitor = asyncio.create_task(self._monitor_loop(), name="connector-monitor")

    async def _monitor_loop(self) -> None:
        """周期检查已启用实例，各实例独立失败。"""
        while True:
            await asyncio.sleep(self._settings.connector_monitor_interval_seconds)
            await asyncio.gather(
                *(self.check_health(item.key) for item in self.catalog() if item.enabled), return_exceptions=True
            )

    async def check_health(self, key: str) -> None:
        """合并并发检查，避免多个页面同时触发重复外部请求。

        Args:
            key: 连接器标识。
        """
        task = self._checks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._check_health(key))
            self._checks[key] = task
        await asyncio.shield(task)

    async def _check_health(self, key: str) -> None:
        """检查与配置更新串行执行，结果只属于当前实例。

        Args:
            key: 连接器标识。
        """
        async with self._locks.setdefault(key, asyncio.Lock()):
            connector = self._instances.get(key)
            desc = self.descriptor(key)
            if connector is None or desc is None or not desc.enabled:
                return
            try:
                async with asyncio.timeout(self._settings.connector_check_timeout_seconds):
                    info = await connector.check_health()
            except TimeoutError:
                info = health_failure("timeout")
            except ConnectorError as exc:
                info = health_failure(exc.code)
            except Exception:
                info = health_failure("check_failed")
            await self._apply_health(key, connector, info)

    async def _apply_health(self, key: str, connector: Connector, info: ConnectorStatusInfo) -> None:
        """保存当前实例的检查结果，保留历史成功时间。

        Args:
            key: 连接器标识。
            connector: 产生结果的实例。
            info: 已脱敏状态。
        """
        from app.models.connector import Connector as ConnectorRow

        desc = self._descriptors.get(key)
        if desc is None or not desc.enabled or connector is not self._instances.get(key):
            return
        checked_at = datetime.now(UTC)
        success_at = checked_at if info.status == "connected" else desc.last_success_at
        try:
            async with self._factory.begin() as session:
                result = await session.execute(
                    update(ConnectorRow)
                    .where(ConnectorRow.key == key, ConnectorRow.enabled.is_(True))
                    .values(
                        status=info.status,
                        auth_status=info.auth_status,
                        last_error=info.message or None,
                        last_error_code=info.error_code,
                        last_checked_at=checked_at,
                        last_success_at=success_at,
                    )
                )
                if result.rowcount == 0:  # type: ignore[attr-defined]
                    return
        except Exception:
            logger.warning("connector_health_save_failed", extra={"key": key})
        desc.status, desc.auth_status = info.status, info.auth_status
        desc.last_checked_at, desc.last_success_at = checked_at, success_at
        desc.last_error, desc.last_error_code = info.message or None, info.error_code

    def _observe(self, spec: ToolSpec, connector: Connector) -> ToolSpec:
        """把真实工具调用结果反馈给监控，保留调用异常给执行层处理。

        Args:
            spec: 原有工具声明。
            connector: 当前实例，用于拒绝配置更新前的迟到结果。

        Returns:
            带健康反馈的工具声明。
        """

        if connector.key in PERSONAL_CONNECTORS:
            # 单个用户授权失效不能改变其他用户看到的全局连接器状态。
            return spec

        async def handler(context: ToolContext, arguments: BaseModel) -> object:
            """记录当前实例的真实调用结果。

            Args:
                context: 可信执行上下文。
                arguments: 校验后的参数。

            Returns:
                原工具结果。
            """
            try:
                result = await spec.handler(context, arguments)
            except Exception as exc:
                info = health_exception(exc)
                async with self._locks.setdefault(connector.key, asyncio.Lock()):
                    await self._apply_health(connector.key, connector, info)
                raise
            auth_status: ConnectorAuthStatus = "authorized"
            if connector.key == FUND_MARKET.key or (
                connector.kind == "cli" and connector.key not in {"tmeet", YANGJIBAO.key}
            ):
                auth_status = "not_required"
            async with self._locks.setdefault(connector.key, asyncio.Lock()):
                await self._apply_health(
                    connector.key, connector, ConnectorStatusInfo(status="connected", auth_status=auth_status)
                )
            return result

        return replace(spec, handler=handler)

    async def close(self) -> None:
        """释放全部连接器资源。"""
        tasks = [task for task in [self._monitor, *self._checks.values()] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._monitor = None
        from app.connectors.authorization import CliAuthorization

        for flow in self._authorizations.values():
            if isinstance(flow, CliAuthorization):
                await flow.close()
        self._authorizations.clear()
        self._yangjibao_scan = None
        for connector in self._instances.values():
            try:
                await connector.aclose()
            except Exception:
                pass
        self._instances.clear()

    async def authorize(self, key: str) -> dict[str, object]:
        """创建或复用当前连接器的授权流程。

        Args:
            key: 连接器标识。

        Returns:
            安全的授权入口和进度。

        Raises:
            ConnectorError: 不支持授权、未启用或网络状态无法确认。
        """
        async with self._auth_locks.setdefault(key, asyncio.Lock()):
            return await self._authorize(key)

    async def authorize_yangjibao(self, scope: ResourceScope, *, poll: bool) -> dict[str, object]:
        """串行更新扫码会话，避免重复取码和重复写入归属。"""
        from app.models.connector import Connector as ConnectorRow

        key = YANGJIBAO.key
        async with self._locks.setdefault(key, asyncio.Lock()):
            connector = self.resolve(key)
            if not isinstance(connector, YangJiBaoConnector):
                raise ConnectorError("not_configured", "请先安装并启用养基宝持仓连接器")
            if connector.owner is not None and connector.owner != scope.user_id:
                raise ApplicationError("CONNECTOR_AUTH_FORBIDDEN", "请由已绑定的工作台账号管理养基宝授权", 403)
            flow = self._yangjibao_scan
            if flow is not None:
                flow.expire()
                if flow.scope != scope:
                    if flow.state == "awaiting_user":
                        raise ApplicationError("CONNECTOR_AUTH_FORBIDDEN", "当前扫码会话属于其他账号或工作空间", 403)
                    flow = None
            if not poll:
                if flow is None or flow.state != "awaiting_user":
                    connector.require_scan_ready()
                    flow = await YangJiBaoScan.start(scope)
                    self._yangjibao_scan = flow
                return flow.snapshot()
            if flow is None:
                return self.authorization_status(key)
            token = await flow.poll()
            if token is None:
                return flow.snapshot()
            async with self._factory.begin() as session:
                row = await session.scalar(select(ConnectorRow).where(ConnectorRow.key == key).with_for_update())
                if row is None or not row.enabled:
                    raise ConnectorError("not_configured", "养基宝持仓连接器已停用")
                bound_owner = row.config.get(BINDING_OWNER_KEY)
                configured_owner = self._settings.yangjibao_owner_user_id
                if configured_owner is None and bound_owner is not None and bound_owner != str(scope.user_id):
                    raise ApplicationError("CONNECTOR_AUTH_FORBIDDEN", "养基宝已绑定到其他工作台账号", 403)
                await asyncio.to_thread(connector.save_scan_token, scope.user_id, token)
                if configured_owner is None:
                    row.config = {**row.config, BINDING_OWNER_KEY: str(scope.user_id)}
            await self._refresh(key)
            desc = self.descriptor(key)
            flow.finish("completed" if desc is not None and desc.auth_status == "authorized" else "failed")
            return flow.snapshot()

    async def _authorize(self, key: str) -> dict[str, object]:
        """在授权锁内复用流程，避免重复点击创建多个登录进程。

        Args:
            key: 连接器标识。

        Returns:
            授权流程快照。
        """
        from app.connectors.authorization import CliAuthorization
        from app.connectors.cli import CliConnector

        connector = self.resolve(key)
        desc = self.descriptor(key)
        if desc is None or not desc.enabled:
            raise ConnectorError("not_configured", "连接器未启用，请先启用连接器")
        if key != "tmeet" or not isinstance(connector, CliConnector):
            raise ConnectorError("not_configured", "该连接器需要在配置中填写凭证")
        flow = self._authorizations.get(key)
        if flow is not None and flow.snapshot()["state"] in {"starting", "awaiting_user"}:
            return await flow.start()
        await self.check_health(key)
        desc = self.descriptor(key)
        if desc is not None and desc.auth_status == "authorized":
            return {
                "auth_url": "",
                "output": "账号已授权",
                "already_logged_in": True,
                "state": "completed",
                "expires_at": None,
            }
        if desc is None or desc.auth_status != "required":
            raise ConnectorError("network_error", "暂时无法确认登录状态，请检查网络后重新检查")
        if flow is not None:
            await flow.close()

        async def finished() -> None:
            """授权进程退出后复查当前连接器。"""
            await self.check_health(key)

        async with self._locks.setdefault(key, asyncio.Lock()):
            if connector is not self.resolve(key):
                raise ConnectorError("not_configured", "连接器配置已更新，请重新开始授权")
            flow = CliAuthorization(str(connector._config.get("cli_path", "tmeet")), finished)
            self._authorizations[key] = flow
        return await flow.start()

    def authorization_status(self, key: str) -> dict[str, object]:
        """读取内存授权进度，供管理员轮询。

        Args:
            key: 连接器标识。

        Returns:
            授权流程快照，包含重启后需重新检查的提示。
        """
        flow = self._authorizations.get(key)
        if flow is not None:
            return flow.snapshot()
        desc = self.descriptor(key)
        authorized = desc is not None and desc.auth_status == "authorized"
        return {
            "auth_url": "",
            "output": "账号已授权" if authorized else "没有进行中的授权，请重新检查连接器状态",
            "already_logged_in": authorized,
            "state": "completed" if authorized else "failed",
            "expires_at": None,
        }

    async def _build_instance(self, row: ConnectorRow) -> Connector | None:
        """按 kind 构造连接器实例。

        Args:
            row: connectors 表行。

        Returns:
            连接器实例；kind 未知返回空。
        """
        if row.key in PERSONAL_CONNECTORS:
            from app.connectors.personal import PersonalConnector

            return PersonalConnector(row.key)
        if row.kind == "api":
            if row.key == FUND_MARKET.key:
                from app.connectors.fund_market import FundMarketConnector

                return FundMarketConnector(self._settings)

            from app.connectors.api import ApiConnector
            from app.models.config import IntegrationConfig

            config = dict(row.config)
            async with self._factory() as session:
                secret = await session.get(IntegrationConfig, f"connector.{row.key}.api_key")
            if secret is not None:
                config["_secrets"] = {"api_key": secret.value}
            return cast(Connector, ApiConnector(key=row.key, config=config, settings=self._settings))
        if row.kind == "cli":
            if row.key == YANGJIBAO.key:
                bound_owner = row.config.get(BINDING_OWNER_KEY)
                return YangJiBaoConnector(self._settings, UUID(bound_owner) if isinstance(bound_owner, str) else None)

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
