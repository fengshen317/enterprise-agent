"""API 连接器测试：声明式工具 → ToolSpec → 执行全链路。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.connectors.api import ApiConnector, ApiToolDef, _extract_field, _fill_template
from app.connectors.base import ConnectorError, ConnectorToolDef, ToolPolicyOverride
from app.connectors.dynamic_args import make_args_model
from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    """构造测试配置。

    Returns:
        不读开发环境文件的配置。
    """
    return Settings(_env_file=None, environment="test")


@pytest.fixture
def api_config() -> dict[str, Any]:
    """构造 API 连接器配置。

    Returns:
        含一个搜索工具的配置。
    """
    return {
        "base_url": "https://api.example.com",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "tools": [
            {
                "name": "search",
                "description": "搜索接口",
                "method": "POST",
                "path": "/search",
                "body_template": {"query": "{query}", "limit": "{limit}"},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                "response_filter": "results",
                "risk": "read",
                "approval": "never",
            }
        ],
    }


def test_api_tool_def_to_connector_def() -> None:
    """ApiToolDef 应正确转换为 ConnectorToolDef。"""
    tool_def = ApiToolDef(
        name="test_tool",
        description="测试工具",
        input_schema={"type": "object"},
        risk="read",
        approval="never",
    )
    connector_def = tool_def.to_connector_def()
    assert connector_def.name == "test_tool"
    assert connector_def.description == "测试工具"
    assert connector_def.risk == "read"
    assert connector_def.approval == "never"


def test_make_args_model_from_schema() -> None:
    """JSON Schema 应生成正确的 pydantic 模型。"""
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    }
    model = make_args_model(schema, "test__tool")
    instance = model(query="hello", limit=5)
    assert instance.query == "hello"
    assert instance.limit == 5


def test_fill_template_string() -> None:
    """模板字符串应替换占位符。"""
    result = _fill_template("hello {name}, you are {age}", {"name": "张三", "age": 30})
    assert result == "hello 张三, you are 30"


def test_fill_template_dict() -> None:
    """模板字典应递归替换。"""
    result = _fill_template(
        {"query": "{q}", "nested": {"key": "{v}"}},
        {"q": "test", "v": "value"},
    )
    assert result == {"query": "test", "nested": {"key": "value"}}


def test_extract_field_simple() -> None:
    """简单字段提取。"""
    data = {"results": [1, 2, 3], "total": 3}
    assert _extract_field(data, "results") == [1, 2, 3]


def test_extract_field_nested() -> None:
    """嵌套字段提取。"""
    data = {"data": {"items": [{"id": 1}]}}
    assert _extract_field(data, "data.items") == [{"id": 1}]


def test_extract_field_missing() -> None:
    """字段不存在时返回原始数据。"""
    data = {"other": 1}
    assert _extract_field(data, "results") == data


async def test_list_tools_from_config(settings: Settings, api_config: dict[str, Any]) -> None:
    """list_tools 应从配置读取声明式工具。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    defs = await connector.list_tools()
    assert len(defs) == 1
    assert defs[0].name == "search"
    assert defs[0].risk == "read"


def test_tool_specs_mapping(settings: Settings, api_config: dict[str, Any]) -> None:
    """tool_specs 应映射为合法 ToolSpec。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    defs = [
        ConnectorToolDef(
            name="search",
            description="搜索",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            risk="read",
            approval="never",
        )
    ]
    policy: dict[str, ToolPolicyOverride] = {}
    specs = connector.tool_specs(defs, policy)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.key == "test_api__search"
    assert spec.risk == "read"
    assert spec.source == "connector"
    assert spec.connector_key == "test_api"


def test_tool_specs_with_policy_override(settings: Settings, api_config: dict[str, Any]) -> None:
    """policy 覆盖应改变风险和审批。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    defs = [
        ConnectorToolDef(
            name="search",
            description="搜索",
            input_schema={"type": "object"},
            risk="external",
            approval="always",
        )
    ]
    policy = {"search": ToolPolicyOverride(risk="read", approval="never")}
    specs = connector.tool_specs(defs, policy)
    assert specs[0].risk == "read"
    assert specs[0].approval == "never"


async def test_call_with_mocked_http(settings: Settings, api_config: dict[str, Any]) -> None:
    """call 应发送 HTTP 请求并返回清洗后的结果。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    connector._client = AsyncMock()

    mock_response = MagicMock()
    mock_response.json.return_value = {"results": [{"title": "test"}]}
    mock_response.raise_for_status = MagicMock()
    connector._client.request = AsyncMock(return_value=mock_response)

    result = await connector.call("search", {"query": "hello", "limit": 5})
    assert result == [{"title": "test"}]


async def test_call_not_found(settings: Settings, api_config: dict[str, Any]) -> None:
    """未定义工具应抛 ConnectorError。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    with pytest.raises(ConnectorError, match="未定义"):
        await connector.call("unknown_tool", {})


async def test_call_http_error(settings: Settings, api_config: dict[str, Any]) -> None:
    """HTTP 错误应映射为 ConnectorError。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    connector._client = AsyncMock()
    connector._client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(ConnectorError, match="API 调用失败"):
        await connector.call("search", {"query": "hello"})


async def test_call_timeout(settings: Settings, api_config: dict[str, Any]) -> None:
    """超时应映射为 timeout ConnectorError。"""
    connector = ApiConnector(key="test_api", config=api_config, settings=settings)
    connector._client = AsyncMock()
    connector._client.request = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    with pytest.raises(ConnectorError, match="超时"):
        await connector.call("search", {"query": "hello"})


async def test_connect_without_base_url(settings: Settings) -> None:
    """缺少 base_url 应返回 error 状态。"""
    connector = ApiConnector(key="test_api", config={}, settings=settings)
    status = await connector.connect()
    assert status.status == "error"
