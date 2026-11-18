"""API、CLI 与 MCP 健康检查中的认证及网络分类。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.capabilities.registry import ToolRegistry
from app.connectors.api import ApiConnector
from app.connectors.base import ConnectorError, health_exception
from app.connectors.cli import CliConnector, CliResult
from app.connectors.registry import ConnectorDescriptor, ConnectorRegistry
from app.core.config import Settings


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


async def test_existing_authorization_never_spawns_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """已授权账号直接返回成功，避免用户重复授权。

    Args:
        monkeypatch: 观察授权进程工厂。
    """
    factory = MagicMock()
    monkeypatch.setattr("app.connectors.authorization.CliAuthorization", factory)
    result = await authorization_registry("authorized").authorize("tmeet")
    assert result["already_logged_in"] and result["state"] == "completed"
    factory.assert_not_called()


async def test_unknown_network_state_does_not_start_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络检查无法确认认证时，保留重试入口。

    Args:
        monkeypatch: 观察授权进程工厂。
    """
    factory = MagicMock()
    monkeypatch.setattr("app.connectors.authorization.CliAuthorization", factory)
    with pytest.raises(ConnectorError) as caught:
        await authorization_registry("unknown").authorize("tmeet")
    assert caught.value.code == "network_error"
    factory.assert_not_called()


async def test_concurrent_authorization_requests_reuse_pending_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发点击只能产生一个等待扫码的流程。

    Args:
        monkeypatch: 替换进程边界，提供可控的授权流程。
    """
    started, release = asyncio.Event(), asyncio.Event()
    result = {"state": "awaiting_user", "auth_url": "https://meeting.tencent.com/", "already_logged_in": False}

    async def start() -> dict[str, object]:
        """等待并发请求进入后返回固定授权进度。

        Returns:
            固定进度。
        """
        started.set()
        await release.wait()
        return result

    flow = MagicMock()
    flow.snapshot.return_value = result
    flow.start = AsyncMock(side_effect=start)
    factory = MagicMock(return_value=flow)
    monkeypatch.setattr("app.connectors.authorization.CliAuthorization", factory)
    registry = authorization_registry("required")
    first = asyncio.create_task(registry.authorize("tmeet"))
    second = asyncio.create_task(registry.authorize("tmeet"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        assert await asyncio.gather(first, second) == [result, result]
        factory.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
