"""养基宝只读同步的账号隔离、数据裁剪和失败行为。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.capabilities.registry import ToolRegistry
from app.capabilities.spec import AgentAssembly
from app.connectors import yangjibao_authorization
from app.connectors.base import ConnectorError
from app.connectors.yangjibao import (
    BINDING_OWNER_KEY,
    PORTFOLIO_DETAIL_TOOL,
    PORTFOLIO_TOOL,
    PortfolioArgs,
    YangJiBaoConnector,
)
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.connector import Connector
from app.models.identity import Workspace
from app.schemas.auth import ResourceScope
from app.schemas.chat import RunCreate
from app.services.agent_assembler import AgentAssembler
from app.services.tools import ToolExecutor
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation
from tests.test_chat_integration import chat as chat


async def test_only_owner_can_assemble_and_read_portfolio(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认和持久化装配遵循账号限制，直接调用也需通过服务端复核。"""
    owner, other, workspace = uuid4(), uuid4(), uuid4()
    settings = Settings(yangjibao_owner_user_id=owner)
    connector = YangJiBaoConnector(settings)
    cli = Mock(
        side_effect=[
            "同步成功",
            json.dumps(
                {
                    "funds": [
                        {
                            "code": "000001",
                            "name": "测试基金",
                            "share": 12345,
                            "cost_nav": 1.5678,
                            "account_id": "secret",
                        },
                        {"code": "000001", "name": "测试基金"},
                    ],
                    "summary": {"total_cost_amount": 12345},
                    "errors": ["private detail"],
                }
            ),
        ]
    )
    monkeypatch.setattr(connector, "_run", cli)
    specs = connector.tool_specs(await connector.list_tools(), {})
    code_key = f"yangjibao__{PORTFOLIO_TOOL}"
    detail_key = f"yangjibao__{PORTFOLIO_DETAIL_TOOL}"
    keys = {code_key, detail_key}
    assert {spec.key for spec in specs} == keys
    registry = ToolRegistry()
    for spec in specs:
        assert spec.risk == "read" and not spec.child_allowed
        registry.register(spec)
    assembler = AgentAssembler(Mock(), registry, settings)
    owner_assembly = assembler.default_assembly(user_id=owner)
    assert keys <= owner_assembly.tool_keys
    assert not keys & assembler.default_assembly(user_id=other).tool_keys
    assert not keys & assembler.default_assembly().tool_keys
    mock_session = Mock()
    mock_session.execute = AsyncMock(return_value=Mock(all=Mock(return_value=[])))
    for viewer, allowed in ((owner, True), (other, False)):
        restored = await assembler._restore(
            mock_session, SimpleNamespace(user_id=viewer, workspace_id=workspace), owner_assembly.snapshot
        )
        assert keys & restored.tool_keys == (keys if allowed else set())
    for spec in specs:
        with pytest.raises(ApplicationError) as forbidden:
            await spec.handler(SimpleNamespace(scope=SimpleNamespace(user_id=other)), PortfolioArgs())
        assert forbidden.value.status_code == 403
    cli.assert_not_called()
    spec = owner_assembly.tool_map[code_key]
    result = await spec.handler(SimpleNamespace(scope=SimpleNamespace(user_id=owner)), PortfolioArgs())
    assert isinstance(result, dict)
    assert result["fund_count"] == 1 and result["funds"] == [{"code": "000001", "name": "测试基金"}]
    assert result["source"] == "养基宝" and isinstance(result["synced_at"], str)
    assert spec.audit(PortfolioArgs(), result) == {"fund_count": 1}
    assert cli.call_args_list[0].args[0] == ("sync", "yjb")
    assert cli.call_args_list[1].args[0] == ("json",)
    await connector.aclose()


async def test_unconfigured_or_failed_sync_does_not_return_holdings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无绑定时不注册工具，上游同步失败时不读取缓存快照。"""
    connector = YangJiBaoConnector(Settings(yangjibao_owner_user_id=None, yangjibao_cli_path=tmp_path / "missing.exe"))
    assert not await connector.list_tools()
    assert (await connector.check_health()).error_code == "not_configured"
    cli = Mock(side_effect=ConnectorError("not_logged_in", "授权失效"))
    monkeypatch.setattr(connector, "_run", cli)
    with pytest.raises(ConnectorError) as error:
        connector._read_holdings()
    assert error.value.code == "not_logged_in"
    cli.assert_called_once()
    with pytest.raises(ApplicationError) as forbidden:
        await connector.call(PORTFOLIO_TOOL, {})
    assert forbidden.value.status_code == 403


async def test_scan_expiry_discards_qr_and_rejects_non_wechat_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """过期清除二维码，签发数据必须来自预期的微信或养基宝地址。"""
    scope = ResourceScope(uuid4(), uuid4())
    request = AsyncMock(
        side_effect=[
            {"id": "private-id", "url": "https://weixin.qq.com.evil.example/qr"},
            {"id": "private-id", "url": "https://weixin.qq.com/qr"},
            {"state": 3, "token": "must-not-be-used"},
        ]
    )
    monkeypatch.setattr(yangjibao_authorization, "_request", request)
    with pytest.raises(ConnectorError, match="可用的微信二维码"):
        await yangjibao_authorization.YangJiBaoScan.start(scope)
    scan = await yangjibao_authorization.YangJiBaoScan.start(scope)
    assert await scan.poll() is None
    assert scan.snapshot()["state"] == "expired"
    assert scan.snapshot()["qr_content"] == ""
    assert await scan.poll() is None
    assert request.await_count == 3


@pytest.mark.integration
async def test_workbench_scan_binds_the_confirming_session_only(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """扫码会话隔离，成功后仅绑定本人并在重建时恢复。"""
    app, client, _ = chat
    owner: ResourceScope = app.state.test_scope
    registry = app.state.connector_registry
    factory = app.state.database.session_factory
    executable = tmp_path / "fundpeek.exe"
    executable.touch()
    credentials = tmp_path / str(owner.user_id) / "credentials.json"
    base_url = "/api/admin/connectors/yangjibao"
    qr_url = "http://weixin.qq.com/q/demo"
    request = AsyncMock(
        side_effect=[
            {"id": "private-qr-id", "url": qr_url},
            {"state": 1},
            {"state": 2, "token": "private-token"},
        ]
    )
    monkeypatch.setattr(yangjibao_authorization, "_request", request)
    monkeypatch.setattr(
        registry,
        "_settings",
        registry._settings.model_copy(
            update={
                "yangjibao_owner_user_id": None,
                "yangjibao_cli_path": executable,
                "yangjibao_config_dir": tmp_path,
            }
        ),
    )

    def cli_output(instance: YangJiBaoConnector, args: tuple[str, ...], timeout: float) -> str:
        """替代 CLI 状态检查，验证真实落盘路径和绑定身份。"""
        assert instance.owner == owner.user_id
        assert args == ("status",)
        assert json.loads(credentials.read_text(encoding="utf8")) == {"yangjibao": {"token": "private-token"}}
        return "yangjibao: authenticated\n"

    monkeypatch.setattr(YangJiBaoConnector, "_run", cli_output)
    try:
        assert (await client.post(f"{base_url}/install")).status_code == 200
        enabled = await client.post(f"{base_url}/enabled", json={"enabled": True})
        assert enabled.status_code == 200 and enabled.json()["auth_action"] == "authorize"
        started = await client.post(f"{base_url}/auth")
        assert started.status_code == 200 and started.headers["Cache-Control"] == "no-store"
        assert started.json()["qr_content"] == qr_url and started.json()["state"] == "awaiting_user"
        assert "private-qr-id" not in started.text and "private-token" not in started.text
        assert (await client.post(f"{base_url}/auth")).json() == started.json()
        assert request.await_count == 1
        assert (await client.get(f"{base_url}/auth/status")).json().get("qr_content") is None

        async with factory() as session:
            slug = await session.scalar(select(Workspace.slug).where(Workspace.id == owner.workspace_id))
        assert slug is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other") as other:
            peer = await add_identity(app, other, workspace=slug, role="admin")
            assert (await other.post(f"{base_url}/auth")).status_code == 403
            assert (await other.post(f"{base_url}/auth/status")).status_code == 403
            assert (
                await other.put(
                    f"{base_url}/config", json={"config": {BINDING_OWNER_KEY: str(peer.user_id)}, "secrets": {}}
                )
            ).status_code == 409
            assert (await client.post(f"{base_url}/auth/status")).json()["state"] == "awaiting_user"
            assert not credentials.exists()

            completed = await client.post(f"{base_url}/auth/status")
            assert completed.status_code == 200 and completed.json()["state"] == "completed"
            assert completed.json()["qr_content"] == "" and "private-token" not in completed.text
            assert (await other.post(f"{base_url}/auth")).status_code == 403
            assert (await other.post(f"{base_url}/auth/status")).status_code == 403
            await registry.refresh("yangjibao")
            assert registry.descriptor("yangjibao").auth_status == "authorized"

            tool = f"yangjibao__{PORTFOLIO_TOOL}"
            assert (await client.get(f"/api/tools/{tool}")).status_code == 200
            assert (await other.get(f"/api/tools/{tool}")).status_code == 404
        assert request.await_count == 3
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == "yangjibao"))
        await registry.refresh("yangjibao")


@pytest.mark.integration
async def test_workbench_enforces_ownership_with_real_runs(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装后仅绑定账号可用，即使共享空间的另一名管理员持有工具快照。"""
    app, client, _ = chat
    owner: ResourceScope = app.state.test_scope
    registry = app.state.connector_registry
    factory = app.state.database.session_factory
    runtime = app.state.chat_runtime
    key = "yangjibao"
    tool = f"{key}__{PORTFOLIO_TOOL}"
    detail_tool = f"{key}__{PORTFOLIO_DETAIL_TOOL}"

    def cli_output(instance: YangJiBaoConnector, args: tuple[str, ...], timeout: float) -> str:
        """隔离真实账号与网络，保留连接器解析和可信运行身份。"""
        return {
            ("status",): "yangjibao: authenticated\n",
            ("sync", "yjb"): "同步成功",
            ("json",): json.dumps(
                {
                    "funds": [
                        {
                            "code": "000001",
                            "name": "测试基金",
                            "share": 12345,
                            "holding_amount": {"available": True, "value": 9876.54},
                            "estimated_change_percent": {"available": True, "value": 0},
                            "latest_nav_change_percent": {"available": False, "value": 3},
                            "quote_time": "2026-11-18 13:30",
                            "cost_amount": {"available": True, "value": 9700},
                            "cost_nav": {"available": False, "value": 0.9},
                            "estimated_amount": {"available": True, "value": 9900},
                            "latest_nav_amount": {"available": True, "value": 9800},
                            "estimated_today_profit_amount": {"available": True, "value": -12.5},
                        }
                    ]
                }
            ),
        }[args]

    async def executor_for(
        requester: httpx.AsyncClient, scope: ResourceScope, assembly: AgentAssembly | None = None
    ) -> ToolExecutor:
        """在真实数据库创建可信运行，使用指定装配模拟历史快照。"""
        conversation = UUID(await new_conversation(requester))
        snapshot = await runtime.create_run(conversation, RunCreate(content="查询持仓", idempotency_key="yjb"), scope)
        await runtime.store.begin_run(snapshot.run.id)
        active_assembly = assembly or await runtime.assembler.assemble_for_run(snapshot.run.id)
        return ToolExecutor(factory, runtime.settings, snapshot.run.id, assembly=active_assembly)

    monkeypatch.setattr(
        registry, "_settings", runtime.settings.model_copy(update={"yangjibao_owner_user_id": owner.user_id})
    )
    monkeypatch.setattr(YangJiBaoConnector, "_run", cli_output)
    try:
        assert (await client.post(f"/api/admin/connectors/{key}/install")).status_code == 200
        enabled = await client.post(f"/api/admin/connectors/{key}/enabled", json={"enabled": True})
        assert enabled.status_code == 200 and enabled.json()["auth_status"] == "authorized"
        assert (await client.get(f"/api/connectors/{key}")).json()["can_view_holdings"] is True
        portfolio = await client.get(f"/api/connectors/{key}/holdings")
        assert portfolio.status_code == 200 and portfolio.headers["Cache-Control"] == "no-store"
        assert portfolio.json()["funds"] == [
            {
                "code": "000001",
                "name": "测试基金",
                "holding_amount": 9876.54,
                "estimated_change_percent": 0.0,
                "latest_nav_change_percent": None,
                "quote_time": "2026-11-18 13:30",
                "nav_date": None,
                "share": 12345,
                "cost_amount": 9700,
                "cost_nav": None,
                "estimated_amount": 9900,
                "latest_nav_amount": 9800,
                "estimated_today_profit_amount": -12.5,
            }
        ]
        assert (await client.get(f"/api/tools/{tool}")).status_code == 200
        assert (await client.get(f"/api/tools/{detail_tool}")).status_code == 200
        owner_executor = await executor_for(client, owner)
        assert {tool, detail_tool} <= owner_executor.assembly.tool_keys
        data = json.loads(await owner_executor.invoke(tool, {}))
        assert data["ok"] and data["data"]["funds"] == [{"code": "000001", "name": "测试基金"}]
        detailed = json.loads(await owner_executor.invoke(detail_tool, {}))
        assert detailed["ok"] and detailed["data"]["source"] == "养基宝"
        assert detailed["data"]["funds"] == portfolio.json()["funds"]
        audit = await client.get(f"/api/runs/{owner_executor.run_id}/tool-calls")
        assert audit.status_code == 200
        assert {record["tool_name"]: json.loads(record["result"]) for record in audit.json()} == {
            tool: {"fund_count": 1},
            detail_tool: {"fund_count": 1},
        }

        async with factory() as session:
            slug = await session.scalar(select(Workspace.slug).where(Workspace.id == owner.workspace_id))
        assert slug is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other") as other:
            peer = await add_identity(app, other, workspace=slug, role="admin")
            assert (await other.get(f"/api/connectors/{key}")).json()["can_view_holdings"] is False
            denied_holdings = await other.get(f"/api/connectors/{key}/holdings")
            assert denied_holdings.status_code == 403 and "9876.54" not in denied_holdings.text
            assert (await other.get(f"/api/tools/{tool}")).status_code == 404
            assert (await other.get(f"/api/tools/{detail_tool}")).status_code == 404
            assert (await other.post(f"/api/admin/tools/{detail_tool}", json={"enabled": False})).status_code == 404
            ordinary = await executor_for(other, peer)
            assert not {tool, detail_tool} & ordinary.assembly.tool_keys
            forged = await executor_for(other, peer, owner_executor.assembly)
            denied = json.loads(await forged.invoke(detail_tool, {}))
            assert not denied["ok"] and denied["error_code"] == "TOOL_NOT_ALLOWED" and "data" not in denied
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == key))
        await registry.refresh(key)
