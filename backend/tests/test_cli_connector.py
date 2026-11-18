"""CLI 登录状态和可选查询参数的回归测试。"""

from copy import deepcopy
from unittest.mock import AsyncMock, call

import pytest

from app.connectors.base import ConnectorError
from app.connectors.catalog import TMEET
from app.connectors.cli import CliConnector, CliResult
from app.connectors.dynamic_args import make_args_model
from app.core.config import Settings


@pytest.fixture
def connector() -> CliConnector:
    """创建独立配置，防止测试修改内置模板。

    Returns:
        不读取开发配置的腾讯会议连接器。
    """
    return CliConnector(key="tmeet", config=deepcopy(TMEET.default_config), settings=Settings(_env_file=None))


@pytest.mark.parametrize("logged_in", [True, False])
async def test_connection_checks_login_even_when_status_exits_zero(connector: CliConnector, logged_in: bool) -> None:
    """tmeet 未登录时 status 也退出零，不能据此展示连接正常。

    Args:
        connector: 独立连接器。
        logged_in: CLI 返回的实际登录状态。
    """
    output = "Logged in\n  AccessToken: valid" if logged_in else "Not logged in. Please use 'tmeet auth login'"
    connector._runner.run = AsyncMock(side_effect=[CliResult("v1.1.0", "", 0), CliResult(output, "", 0)])
    result = await connector.connect()
    assert result.status == ("connected" if logged_in else "error")
    assert connector._runner.run.await_args_list == [
        call(["tmeet", "--version"], timeout_seconds=5),
        call(["tmeet", "auth", "status"], timeout_seconds=5),
    ]
    if not logged_in:
        assert "账号授权" in result.message


async def test_generic_cli_does_not_require_tmeet_auth() -> None:
    """其他 CLI 不应被强制执行腾讯会议专用认证命令。"""
    generic = CliConnector(key="other", config={"cli_path": "other"}, settings=Settings(_env_file=None))
    generic._runner.run = AsyncMock(return_value=CliResult("v1", "", 0))
    assert (await generic.connect()).status == "connected"
    generic._runner.run.assert_awaited_once()


async def test_auth_status_returns_structured_state_without_account_details(connector: CliConnector) -> None:
    """auth status 是文本输出，认证工具只返回状态，避免 JSON 解析失败与凭证信息回显。

    Args:
        connector: 独立连接器。
    """
    connector._runner.run = AsyncMock(return_value=CliResult("Logged in\n  AccessToken: private-detail", "", 0))
    assert await connector.call("auth_status", {}) == {"logged_in": True}


async def test_auth_status_rejects_unknown_output(connector: CliConnector) -> None:
    """无法识别的状态不能冒充已登录。

    Args:
        connector: 独立连接器。
    """
    connector._runner.run = AsyncMock(return_value=CliResult("unexpected output", "", 0))
    with pytest.raises(ConnectorError) as caught:
        await connector.call("auth_status", {})
    assert caught.value.code == "invalid_output"


@pytest.mark.parametrize("query", [None, "季度评审"])
async def test_search_omits_unset_options(connector: CliConnector, query: str | None) -> None:
    """空时间范围必须省略参数，让 CLI 使用默认窗口，不能发送字符串 None。

    Args:
        connector: 独立连接器。
        query: 可选的真实搜索词。
    """
    spec = next(item for item in await connector.list_tools() if item.name == "search_meetings")
    args = make_args_model(spec.input_schema, "search").model_validate({"query": query}).model_dump()
    connector._runner.run_json = AsyncMock(return_value={"meetings": []})
    await connector.call("search_meetings", args)
    expected = ["tmeet", "meeting", "search"]
    if query is not None:
        expected += ["--query", query]
    expected += ["--page-size", "30", "--format", "json"]
    connector._runner.run_json.assert_awaited_once_with(expected, 30)
