"""内置连接器目录与安装测试。"""

from __future__ import annotations

from app.connectors.catalog import BUILTIN_CONNECTORS, get_builtin


def test_builtin_count() -> None:
    """应内置 tmeet 和 tavily 两个连接器。"""
    assert len(BUILTIN_CONNECTORS) >= 2
    keys = {item.key for item in BUILTIN_CONNECTORS}
    assert "tmeet" in keys
    assert "tavily_search" in keys


def test_builtin_fields() -> None:
    """每个内置连接器应有完整字段。"""
    for item in BUILTIN_CONNECTORS:
        assert item.key
        assert item.kind in ("cli", "mcp", "api")
        assert item.name
        assert item.description
        assert item.icon


def test_get_builtin() -> None:
    """按 key 查找内置连接器。"""
    tmeet = get_builtin("tmeet")
    assert tmeet is not None
    assert tmeet.kind == "cli"
    assert tmeet.icon == "📹"

    tavily = get_builtin("tavily_search")
    assert tavily is not None
    assert tavily.kind == "api"
    assert tavily.icon == "🔍"

    assert get_builtin("nonexistent") is None


def test_tmeet_tools() -> None:
    """tmeet 应包含完整工具集（meeting/minutes/record）。"""
    tmeet = get_builtin("tmeet")
    assert tmeet is not None
    tools = tmeet.default_config.get("tools", [])
    assert len(tools) >= 10
    tool_names = {t["name"] for t in tools if isinstance(t, dict)}
    assert "search_meetings" in tool_names
    assert "get_meeting" in tool_names
    assert "search_minutes" in tool_names
    assert "get_minutes" in tool_names
    assert "get_short_summary" in tool_names
    assert "search_records" in tool_names
    assert "get_record_content" in tool_names


def test_tavily_tools() -> None:
    """tavily 应包含 1 个搜索工具。"""
    tavily = get_builtin("tavily_search")
    assert tavily is not None
    tools = tavily.default_config.get("tools", [])
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "search"
    assert tool["method"] == "POST"
    assert tool["path"] == "/search"


def test_tavily_body_mapping() -> None:
    """tavily 的 api_key 应映射到 body。"""
    tavily = get_builtin("tavily_search")
    assert tavily is not None
    mapping = tavily.default_config.get("body_mapping", {})
    assert mapping.get("api_key") == "api_key"


def test_tavily_config_fields() -> None:
    """tavily 应有 base_url 和 api_key 两个配置字段。"""
    tavily = get_builtin("tavily_search")
    assert tavily is not None
    keys = {f["key"] for f in tavily.config_fields}
    assert "base_url" in keys
    assert "api_key" in keys


def test_tmeet_config_fields() -> None:
    """tmeet 应有 cli_path 配置字段。"""
    tmeet = get_builtin("tmeet")
    assert tmeet is not None
    keys = {f["key"] for f in tmeet.config_fields}
    assert "cli_path" in keys
