"""API、CLI 与 MCP 健康检查中的认证及网络分类。"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from app.capabilities.registry import ToolRegistry
from app.connectors.api import ApiConnector
from app.connectors.authorization import CliAuthorization
from app.connectors.base import ConnectorError, health_exception
from app.connectors.cli import CliConnector, CliResult
from app.connectors.personal_cli import PersonalCliConnector
from app.connectors.registry import ConnectorDescriptor, ConnectorRegistry
from app.core.config import Settings
from app.schemas.auth import ResourceScope


async def test_missing_api_key_requires_configuration() -> None:
    """缺少必需密钥时给出配置提示。"""
    connector = ApiConnector(
        key="tavily_search",
        config={"base_url": "https://example.invalid"},
        settings=Settings(_env_file=None),
    )
    status = await connector.check_health()
    assert status.auth_status == "required" and status.error_code == "missing_api_key"


async def test_api_client_creation_does_not_verify_credentials() -> None:
    """配置完整但缺少只读验证路径时，认证保持待确认。"""
    connector = ApiConnector(
        key="tavily_search",
        config={"base_url": "https://example.invalid", "_secrets": {"api_key": "test-key"}},
        settings=Settings(_env_file=None),
    )
    status = await connector.check_health()
    assert status.auth_status == "unknown" and status.status == "degraded"
    assert connector._get_secret("api_key") == "test-key"


@pytest.mark.parametrize(("health_path", "expected_path"), [("", "/usage"), ("/custom", "/custom")])
async def test_official_tavily_uses_readonly_usage_path(
    monkeypatch: pytest.MonkeyPatch, health_path: str, expected_path: str
) -> None:
    """官方地址默认验证密钥，并保留已指定的健康检查路径。"""

    def transport(request: httpx.Request) -> httpx.Response:
        """检查验证请求不执行搜索。"""
        assert request.method == "GET" and request.url.path == expected_path
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.connectors.api.httpx.AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(transport)),
    )
    connector = ApiConnector(
        key="tavily_search",
        config={
            "base_url": "https://api.tavily.com/",
            "health_path": health_path,
            "_secrets": {"api_key": "test-key"},
        },
        settings=Settings(_env_file=None),
    )
    status = await connector.check_health()
    assert status.status == "connected" and status.auth_status == "authorized"


@pytest.mark.parametrize("status_code, expected", [(200, "authorized"), (401, "required"), (503, "unknown")])
async def test_readonly_api_check_classifies_response(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected: str,
) -> None:
    """只读健康接口使用已保存密钥，认证和服务错误有独立状态。

    Args:
        monkeypatch: 注入受控 HTTP 传输。
        status_code: 服务返回码。
        expected: 预期认证状态。
    """

    def transport(request: httpx.Request) -> httpx.Response:
        """验证探测使用 GET 和后台密钥。

        Args:
            request: 健康请求。

        Returns:
            受控服务状态。
        """
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(status_code, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.connectors.api.httpx.AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(transport)),
    )
    connector = ApiConnector(
        key="test_api",
        config={"base_url": "https://example.invalid", "health_path": "/health", "api_key": "test-key"},
        settings=Settings(_env_file=None),
    )
    assert (await connector.check_health()).auth_status == expected


async def test_tmeet_refresh_network_failure_stays_unknown() -> None:
    """CLI 刷新时出现 DNS 错误，即使伴随登录提示也先报告网络异常。"""
    connector = CliConnector(key="tmeet", config={}, settings=Settings(_env_file=None))
    connector._runner.run = AsyncMock(
        side_effect=[
            CliResult("v1", "", 0),
            CliResult("Not logged in", "lookup work.medialab.qq.com: no such host", 0),
        ]
    )
    result = await connector.check_health()
    assert result.auth_status == "unknown" and result.error_code == "network_error"


def test_grouped_mcp_authentication_failure_is_classified() -> None:
    """MCP SDK 任务组包装的 401 仍能识别为需要配置认证。"""
    response = httpx.Response(401, request=httpx.Request("GET", "https://example.invalid"))
    exception = httpx.HTTPStatusError("private-response", request=response.request, response=response)
    result = health_exception(ExceptionGroup("transport", [exception]))
    assert result.auth_status == "required" and result.error_code == "auth_failed"
    assert "private" not in result.message


def authorization_registry(auth_status: str) -> ConnectorRegistry:
    """构造授权入口的隔离注册表。

    Args:
        auth_status: 已记录的认证状态。

    Returns:
        外部检查已隔离的注册表。
    """
    settings = Settings(_env_file=None)
    registry = ConnectorRegistry(MagicMock(), ToolRegistry(), settings)
    registry._instances["tmeet"] = CliConnector(key="tmeet", config={}, settings=settings)
    registry._descriptors["tmeet"] = ConnectorDescriptor(
        key="tmeet",
        kind="cli",
        name="腾讯会议",
        description="",
        icon="",
        enabled=True,
        status="connected",
        tool_count=1,
        auth_status=auth_status,
    )
    registry.check_health = AsyncMock()
    return registry


async def test_global_authorization_does_not_authorize_member() -> None:
    """主机或其他成员的已登录状态不能授权给当前成员。"""
    registry = authorization_registry("authorized")
    scope = ResourceScope(uuid4(), uuid4())
    result = registry.personal_auth.status("tmeet", scope)
    assert not result["already_logged_in"] and result["state"] == "failed"


async def test_legacy_cli_never_used_for_personal_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧全局 CLI 实例不能作为个人登录的后备入口。"""
    monkeypatch.setattr("app.connectors.personal_authorization.require_membership", AsyncMock())
    registry = authorization_registry("authorized")
    with pytest.raises(ConnectorError) as caught:
        await registry.personal_auth.authorize("tmeet", ResourceScope(uuid4(), uuid4()))
    assert caught.value.code == "not_configured"


async def test_concurrent_authorization_requests_reuse_pending_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """同一成员并发点击只创建一份个人目录和扫码流程。"""
    started, release = asyncio.Event(), asyncio.Event()
    result = {"state": "awaiting_user", "auth_url": "https://meeting.tencent.com/", "already_logged_in": False}

    async def start(self: CliAuthorization) -> dict[str, object]:
        """等待两个请求交错，不启动真正的 CLI。"""
        started.set()
        await release.wait()
        return result

    monkeypatch.setattr("app.connectors.personal_authorization.require_membership", AsyncMock())
    monkeypatch.setattr(CliAuthorization, "start", start)
    registry = authorization_registry("unknown")
    registry._settings.personal_connector_data_dir = tmp_path
    registry._instances["tmeet"] = PersonalCliConnector("tmeet", {}, registry._settings)
    scope = ResourceScope(uuid4(), uuid4())
    first = asyncio.create_task(registry.personal_auth.authorize("tmeet", scope))
    second = asyncio.create_task(registry.personal_auth.authorize("tmeet", scope))
    try:
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        assert await asyncio.gather(first, second) == [result, result]
        assert len(registry.personal_auth.flows) == 1
        assert await asyncio.to_thread(lambda: len(list(tmp_path.rglob("tmeet/*")))) == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await registry.close()
