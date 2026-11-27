"""连接器健康监控的并发、工具反馈与数据库回归验证。"""

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capabilities.registry import ToolRegistry
from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors.base import Connector, ConnectorError, ConnectorStatusInfo, health_failure
from app.connectors.registry import ConnectorDescriptor, ConnectorRegistry
from app.core.config import Settings
from app.models.connector import Connector as ConnectorRow
from tests.test_chat_integration import ControlledProvider

pytest_plugins = ["tests.test_chat_integration"]


class EmptyArguments(BaseModel):
    """无参数的连接器测试工具。"""


def monitor_fixture() -> tuple[ConnectorRegistry, MagicMock, MagicMock]:
    """构造隔离的注册表，数据库写入使用受控会话。

    Returns:
        注册表、连接器替身和会话工厂。
    """
    factory = MagicMock()
    factory.begin.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(spec=AsyncSession))
    factory.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    factory.begin.return_value.__aenter__.return_value.execute.return_value = MagicMock(rowcount=1)
    registry = ConnectorRegistry(
        factory,
        ToolRegistry(),
        Settings(connector_monitor_enabled=True, connector_monitor_interval_seconds=60),
    )
    connector = MagicMock(spec=Connector)
    connector.key = "monitor_api"
    connector.kind = "api"
    connector.check_health = AsyncMock(return_value=ConnectorStatusInfo(status="connected", auth_status="authorized"))
    connector.aclose = AsyncMock()
    registry._instances[connector.key] = connector
    registry._descriptors[connector.key] = ConnectorDescriptor(
        key=connector.key,
        kind="api",
        name="测试连接器",
        description="",
        icon="",
        enabled=True,
        status="connected",
        tool_count=1,
        auth_status="authorized",
    )
    return registry, connector, factory


def tool_spec(connector: MagicMock, handler: AsyncMock) -> ToolSpec:
    """保留真实工具声明，仅替换外部调用和执行上下文。

    Args:
        connector: 当前连接器。
        handler: 可控的底层执行函数。

    Returns:
        尚未注册的原始工具声明。
    """
    return ToolSpec(
        key="monitor_api__list_meetings",
        version=1,
        display_name="会议列表",
        description="读取会议列表",
        args_model=EmptyArguments,
        risk="read",
        approval="never",
        timeout_seconds=10,
        quota_per_run=10,
        handler=handler,
        audit=MagicMock(return_value={}),
        source="connector",
        connector_key=connector.key,
    )


@pytest.mark.parametrize("code", ["not_logged_in", "auth_failed"])
async def test_unauthorized_check_requires_authorization(code: str) -> None:
    """未授权检查立即提供授权操作，异常正文不会进入状态。

    Args:
        code: 已识别的认证失败分类。
    """
    registry, connector, _ = monitor_fixture()
    connector.check_health.side_effect = ConnectorError(code, "private-token")
    await registry.check_health(connector.key)
    desc = registry.descriptor(connector.key)
    assert desc is not None
    assert desc.auth_status == "required"
    assert desc.auth_action == "configure"
    assert desc.last_error_code == code
    assert desc.last_checked_at is not None
    assert "private-token" not in str(desc.last_error)


async def test_network_failure_preserves_previous_success() -> None:
    """网络故障将认证置为待确认，并保留上次成功的时间。"""
    registry, connector, _ = monitor_fixture()
    await registry.check_health(connector.key)
    desc = registry.descriptor(connector.key)
    assert desc is not None
    last_success = desc.last_success_at
    assert last_success is not None
    connector.check_health.side_effect = ConnectorError("network_error", "private-response")
    await registry.check_health(connector.key)
    assert desc.auth_status == "unknown"
    assert desc.auth_action == "retry"
    assert desc.last_error_code == "network_error"
    assert desc.last_success_at == last_success
    assert desc.last_checked_at is not None and desc.last_checked_at >= last_success


async def test_concurrent_checks_share_one_probe() -> None:
    """多个调用者共享同一次探测，取消调用者不会取消底层检查。"""
    registry, connector, _ = monitor_fixture()
    started, release = asyncio.Event(), asyncio.Event()

    async def probe() -> ConnectorStatusInfo:
        """在所有调用者进入前保持检查未完成。

        Returns:
            授权成功状态。
        """
        started.set()
        await release.wait()
        return ConnectorStatusInfo(status="connected", auth_status="authorized")

    connector.check_health.side_effect = probe
    callers = [asyncio.create_task(registry.check_health(connector.key)) for _ in range(8)]
    try:
        await asyncio.wait_for(started.wait(), 1)
        callers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await callers[0]
        release.set()
        await asyncio.gather(*callers[1:])
        connector.check_health.assert_awaited_once()
        assert registry._checks[connector.key].done()
    finally:
        release.set()
        await asyncio.gather(*callers, return_exceptions=True)
        await registry.close()


async def test_disabled_descriptor_skips_probe() -> None:
    """停用视图即使尚有待清理实例也不会触发外部探测。"""
    registry, connector, _ = monitor_fixture()
    desc = registry.descriptor(connector.key)
    assert desc is not None
    desc.enabled, desc.status = False, "disabled"
    await registry.check_health(connector.key)
    connector.check_health.assert_not_awaited()
    assert desc.status == "disabled"
    assert desc.auth_action is None


async def test_timeout_has_retry_action_and_preserves_success() -> None:
    """超时按固定分类落状态，并保留最近成功记录。"""
    registry, connector, _ = monitor_fixture()
    registry._settings.connector_check_timeout_seconds = 0.01
    desc = registry.descriptor(connector.key)
    assert desc is not None
    previous = datetime(2026, 1, 1, tzinfo=UTC)
    desc.last_success_at = previous

    async def blocked() -> ConnectorStatusInfo:
        """等待取消以验证注册表的超时边界。

        Returns:
            永不产生的成功结果。
        """
        await asyncio.Event().wait()
        return ConnectorStatusInfo(status="connected")

    connector.check_health.side_effect = blocked
    await registry.check_health(connector.key)
    assert desc.last_error_code == "timeout"
    assert desc.auth_status == "unknown"
    assert desc.auth_action == "retry"
    assert desc.last_success_at == previous


async def test_late_old_instance_result_is_ignored() -> None:
    """配置重建后的旧实例迟到结果不能覆盖新状态或写数据库。"""
    registry, old, factory = monitor_fixture()
    replacement = MagicMock(spec=Connector)
    registry._instances[old.key] = replacement
    desc = registry.descriptor(old.key)
    assert desc is not None
    await registry._apply_health(old.key, old, health_failure("not_logged_in"))
    assert desc.status == "connected"
    assert desc.auth_status == "authorized"
    assert desc.last_checked_at is None
    factory.begin.assert_not_called()


async def test_tool_failure_immediately_updates_health_and_reraises() -> None:
    """真实工具异常即时触发认证状态更新，并原样交给执行层处理。"""
    registry, connector, _ = monitor_fixture()
    error = ConnectorError("not_logged_in", "private-token")
    handler = AsyncMock(side_effect=error)
    spec = registry._observe(tool_spec(connector, handler), connector)
    context = cast(ToolContext, MagicMock())
    with pytest.raises(ConnectorError) as failure:
        await spec.handler(context, EmptyArguments())
    assert failure.value is error
    desc = registry.descriptor(connector.key)
    assert desc is not None
    assert desc.auth_status == "required"
    assert desc.auth_action == "configure"
    assert desc.last_error_code == "not_logged_in"
    connector.check_health.assert_not_awaited()
    handler.assert_awaited_once()


async def test_tool_success_recovers_health_without_extra_probe() -> None:
    """成功工具调用恢复授权状态，返回值和参数传递保持一致。"""
    registry, connector, _ = monitor_fixture()
    await registry._apply_health(connector.key, connector, health_failure("not_logged_in"))
    result = {"meetings": []}
    handler = AsyncMock(return_value=result)
    spec = registry._observe(tool_spec(connector, handler), connector)
    context, args = cast(ToolContext, MagicMock()), EmptyArguments()
    assert await spec.handler(context, args) is result
    handler.assert_awaited_once_with(context, args)
    desc = registry.descriptor(connector.key)
    assert desc is not None
    assert desc.status == "connected" and desc.auth_status == "authorized"
    assert desc.last_error is None and desc.last_error_code is None
    assert desc.last_success_at == desc.last_checked_at
    connector.check_health.assert_not_awaited()


async def test_late_tool_failure_keeps_replacement_health() -> None:
    """运行中的旧工具在实例更换后失败，无法污染新实例的状态。"""
    registry, connector, factory = monitor_fixture()
    started, release = asyncio.Event(), asyncio.Event()

    async def failing(context: ToolContext, arguments: BaseModel) -> object:
        """延迟工具失败，给热更新留出确定的交错窗口。

        Args:
            context: 工具上下文。
            arguments: 工具参数。

        Raises:
            ConnectorError: 模拟旧账号过期。
        """
        started.set()
        await release.wait()
        raise ConnectorError("not_logged_in", "private-old-response")

    spec = registry._observe(tool_spec(connector, AsyncMock(side_effect=failing)), connector)
    calling = asyncio.create_task(spec.handler(cast(ToolContext, MagicMock()), EmptyArguments()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        registry._instances[connector.key] = MagicMock(spec=Connector)
        release.set()
        with pytest.raises(ConnectorError):
            await calling
        desc = registry.descriptor(connector.key)
        assert desc is not None and desc.auth_status == "authorized"
        factory.begin.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(calling, return_exceptions=True)


async def test_close_cancels_monitor_and_probe_before_resource_close() -> None:
    """关闭先取消探测和监控，再释放资源，重复启动不会产生多余任务。"""
    registry, connector, _ = monitor_fixture()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked() -> ConnectorStatusInfo:
        """记录取消完成时机，资源只能在探测退出后关闭。

        Returns:
            永不产生的结果。
        """
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return ConnectorStatusInfo(status="connected")

    async def closed() -> None:
        """检查释放资源时外部探测已退出。"""
        assert cancelled.is_set()

    connector.check_health.side_effect = blocked
    connector.aclose.side_effect = closed
    registry.start()
    monitor = registry._monitor
    assert monitor is not None
    registry.start()
    assert registry._monitor is monitor
    checking = asyncio.create_task(registry.check_health(connector.key))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await registry.close()
        assert monitor.done()
        assert registry._monitor is None
        assert all(task.done() for task in registry._checks.values())
        assert registry.resolve(connector.key) is None
        connector.aclose.assert_awaited_once()
        await registry.close()
        connector.aclose.assert_awaited_once()
    finally:
        await registry.close()
        await asyncio.gather(checking, return_exceptions=True)


@pytest.mark.parametrize("entrypoint", ["load", "refresh"])
async def test_registered_tools_include_health_feedback(entrypoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动与热更新注册的工具都包含健康反馈。

    Args:
        entrypoint: 注册表生命周期入口。
        monkeypatch: 替换外部实例构建，保留完整注册逻辑。
    """
    registry, connector, factory = monitor_fixture()
    row = ConnectorRow(
        key=connector.key,
        kind="api",
        name="测试连接器",
        description="",
        icon="",
        enabled=True,
        config={},
        discovered_tools=[],
    )
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [row]
    rows.scalar_one_or_none.return_value = row
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = rows
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    connector.connect = AsyncMock(return_value=ConnectorStatusInfo(status="connected", auth_status="authorized"))
    connector.list_tools = AsyncMock(return_value=[])
    connector.tool_specs.return_value = [
        tool_spec(connector, AsyncMock(side_effect=ConnectorError("not_logged_in", "private-token")))
    ]
    monkeypatch.setattr(registry, "_build_instance", AsyncMock(return_value=connector))
    if entrypoint == "load":
        await registry.load()
    else:
        await registry.refresh(connector.key)
    registered = registry._tool_registry.resolve("monitor_api__list_meetings")
    assert registered is not None
    with pytest.raises(ConnectorError):
        await registered.handler(cast(ToolContext, MagicMock()), EmptyArguments())
    desc = registry.descriptor(connector.key)
    assert desc is not None and desc.auth_status == "required"
    assert desc.last_error_code == "not_logged_in"
    assert desc.auth_action == "configure"


async def test_refresh_waits_until_probe_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置重建必须等待当前探测结束，避免关闭正在使用的实例。

    Args:
        monkeypatch: 仅替换锁内重建步骤，隔离数据库和实例构建。
    """
    registry, connector, _ = monitor_fixture()
    started, release = asyncio.Event(), asyncio.Event()

    async def probe() -> ConnectorStatusInfo:
        """保持外部探测未完成以验证重建锁。

        Returns:
            当前实例的成功结果。
        """
        started.set()
        await release.wait()
        return ConnectorStatusInfo(status="connected", auth_status="authorized")

    connector.check_health.side_effect = probe
    rebuilding = AsyncMock()
    monkeypatch.setattr(registry, "_refresh", rebuilding)
    checking = asyncio.create_task(registry.check_health(connector.key))
    refreshing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        refreshing = asyncio.create_task(registry.refresh(connector.key))
        await asyncio.sleep(0)
        rebuilding.assert_not_awaited()
        release.set()
        await asyncio.gather(checking, refreshing)
        rebuilding.assert_awaited_once_with(connector.key)
    finally:
        release.set()
        await registry.close()
        await asyncio.gather(checking, *([refreshing] if refreshing is not None else []), return_exceptions=True)


@pytest.mark.integration
async def test_health_persistence_and_disable_during_probe(
    chat: tuple[FastAPI, AsyncClient, ControlledProvider],
) -> None:
    """真实数据库保存健康时间，并拒绝探测覆盖并发停用的配置。

    Args:
        chat: 复用独立测试库应用生命周期。
    """
    app, _, _ = chat
    factory: async_sessionmaker[AsyncSession] = app.state.database.session_factory
    key = "monitor_test_" + uuid4().hex
    registry, connector, _ = monitor_fixture()
    registry._factory = factory
    registry._instances[key] = registry._instances.pop(connector.key)
    desc = registry._descriptors.pop(connector.key)
    connector.key = desc.key = key
    registry._descriptors[key] = desc
    started, release = asyncio.Event(), asyncio.Event()
    checking: asyncio.Task[None] | None = None
    try:
        async with factory.begin() as session:
            session.add(ConnectorRow(key=key, kind="api", name="监控回归测试", enabled=True))
        await registry.check_health(key)
        async with factory() as session:
            row = (await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))).scalar_one()
            assert row.status == "connected"
            assert row.auth_status == "authorized"
            assert row.last_checked_at is not None and row.last_success_at == row.last_checked_at
            previous_success = row.last_success_at

        async def blocked() -> ConnectorStatusInfo:
            """数据库停用提交前保持探测处于外部等待阶段。

            Returns:
                账号失效状态。
            """
            started.set()
            await release.wait()
            return health_failure("not_logged_in")

        connector.check_health.side_effect = blocked
        checking = asyncio.create_task(registry.check_health(key))
        await asyncio.wait_for(started.wait(), 2)
        async with factory.begin() as session:
            await session.execute(
                update(ConnectorRow).where(ConnectorRow.key == key).values(enabled=False, status="disabled")
            )
        release.set()
        await checking
        async with factory() as session:
            row = (await session.execute(select(ConnectorRow).where(ConnectorRow.key == key))).scalar_one()
            assert not row.enabled and row.status == "disabled"
            assert row.auth_status == "authorized"
            assert row.last_success_at == previous_success
            assert row.last_error_code is None
        await registry.refresh(key)
        disabled = registry.descriptor(key)
        assert disabled is not None and not disabled.enabled and disabled.status == "disabled"
        assert registry.resolve(key) is None
        connector.check_health.reset_mock()
        await registry.check_health(key)
        connector.check_health.assert_not_awaited()
    finally:
        release.set()
        await registry.close()
        if checking is not None:
            await asyncio.gather(checking, return_exceptions=True)
        async with factory.begin() as session:
            await session.execute(delete(ConnectorRow).where(ConnectorRow.key == key))
