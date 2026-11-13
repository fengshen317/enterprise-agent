"""MCP 连接器测试：工具映射与执行逻辑（mock MCP SDK）。"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.base import ConnectorToolDef, ToolPolicyOverride
from app.connectors.mcp import McpConnector
from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    """构造测试配置。

    Returns:
        不读开发环境文件的配置。
    """
    return Settings(_env_file=None, environment="test")


@pytest.fixture
def mcp_config() -> dict[str, Any]:
    """构造 MCP 连接器配置。

    Returns:
        stdio 传输配置。
    """
    return {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "some-mcp-server"],
    }


def test_tool_specs_mapping(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """MCP 工具定义应映射为 ToolSpec。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)
    defs = [
        ConnectorToolDef(
            name="search_books",
            description="搜索图书",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            risk="external",
            approval="always",
        )
    ]
    policy: dict[str, ToolPolicyOverride] = {}
    specs = connector.tool_specs(defs, policy)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.key == "test_mcp__search_books"
    assert spec.risk == "external"
    assert spec.approval == "always"
    assert spec.source == "connector"
    assert spec.connector_key == "test_mcp"


def test_tool_specs_with_policy(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """policy 覆盖应改变风险级别。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)
    defs = [
        ConnectorToolDef(
            name="read_data",
            description="读取数据",
            input_schema={"type": "object"},
            risk="external",
            approval="always",
        )
    ]
    policy = {"read_data": ToolPolicyOverride(risk="read", approval="never")}
    specs = connector.tool_specs(defs, policy)
    assert specs[0].risk == "read"
    assert specs[0].approval == "never"


def test_tool_specs_input_schema_override(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """input_schema_override 应保存原始 schema。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    defs = [
        ConnectorToolDef(
            name="tool",
            description="工具",
            input_schema=schema,
            risk="external",
            approval="always",
        )
    ]
    specs = connector.tool_specs(defs, {})
    assert specs[0].input_schema_override == schema


def test_config_fields(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """config_fields 应返回 MCP 配置字段。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)
    fields = connector.config_fields()
    keys = {f.key for f in fields}
    assert "transport" in keys
    assert "command" in keys
    assert "url" in keys


async def test_list_tools_with_mock_session(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """list_tools 应从 MCP session 获取工具。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)

    # 模拟 MCP session
    mock_tool = MagicMock()
    mock_tool.name = "get_weather"
    mock_tool.description = "获取天气"
    mock_tool.inputSchema = {"type": "object", "properties": {"city": {"type": "string"}}}

    mock_result = MagicMock()
    mock_result.tools = [mock_tool]

    mock_session = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=mock_result)
    connector._session = mock_session

    defs = await connector.list_tools()
    assert len(defs) == 1
    assert defs[0].name == "get_weather"
    assert defs[0].description == "获取天气"


async def test_call_with_mock_session(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """call 应通过 MCP session 执行工具调用。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)

    mock_result = MagicMock()
    mock_result.structuredContent = {"temperature": 25}
    mock_result.content = []

    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)
    connector._session = mock_session

    result = await connector.call("get_weather", {"city": "北京"})
    assert result == {"temperature": 25}


async def test_call_fallback_to_text(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """structuredContent 为空时退化为 text 内容。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)

    mock_block = MagicMock()
    mock_block.text = "晴天，25度"
    mock_result = MagicMock()
    mock_result.structuredContent = None
    mock_result.content = [mock_block]

    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)
    connector._session = mock_session

    result = await connector.call("get_weather", {"city": "北京"})
    assert result == {"text": "晴天，25度"}


async def test_connect_without_mcp_sdk(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """MCP SDK 未安装时 connect 不启动子进程，工具列表为空。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)
    # sys.modules 置空会让 import mcp 抛出 ImportError，即使环境里已经安装了 SDK。
    with patch.dict(sys.modules, {"mcp": None}):
        await connector.connect()
        defs = await connector.list_tools()
    assert defs == []


async def test_aclose(settings: Settings, mcp_config: dict[str, Any]) -> None:
    """aclose 应释放 session 和 streams。"""
    connector = McpConnector(key="test_mcp", config=mcp_config, settings=settings)

    mock_session = AsyncMock()
    mock_session.__aexit__ = AsyncMock()
    connector._session = mock_session

    mock_streams = AsyncMock()
    mock_streams.__aexit__ = AsyncMock()
    connector._streams = mock_streams

    await connector.aclose()
    assert connector._session is None
    assert connector._streams is None
