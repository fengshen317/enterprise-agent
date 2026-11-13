"""内置连接器目录：预置连接器模板，前端可"一键安装"。

每个模板含 kind / 名称 / 描述 / 图标 / 配置字段声明 / 默认配置。
安装时写入 connectors 表，后续配置、密钥、启停流程与自定义连接器一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BuiltinConnector:
    """内置连接器模板。"""

    key: str
    kind: str  # "cli" | "mcp" | "api"
    name: str
    description: str
    icon: str
    default_config: dict[str, Any] = field(default_factory=dict)
    # 前端动态表单字段（与 ConfigField 结构一致）
    config_fields: tuple[dict[str, Any], ...] = ()


# 腾讯会议 tmeet CLI：真实可用（需本机安装 tmeet 并登录）
TMEET_TIME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": "开始时间 ISO 8601，含时区"},
        "end": {"type": "string", "description": "结束时间 ISO 8601，含时区"},
        "page_size": {"type": "integer", "default": 30},
    },
    "required": [],
}
TMEET_MEETING_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meeting_id": {"type": "string", "description": "会议 ID"},
    },
    "required": ["meeting_id"],
}
TMEET_MEETING_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词"},
        "query_field": {"type": "string", "default": "all", "description": "搜索字段 subject/creator/note/all"},
        "start": {"type": "string", "description": "开始时间 ISO 8601"},
        "end": {"type": "string", "description": "结束时间 ISO 8601"},
        "page_size": {"type": "integer", "default": 30},
    },
    "required": [],
}
TMEET_MINUTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "minute_id": {"type": "string", "description": "会议纪要 ID"},
    },
    "required": ["minute_id"],
}
TMEET_SHORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "minute_id": {"type": "string", "description": "会议纪要 ID"},
        "page_size": {"type": "integer", "default": 100},
    },
    "required": ["minute_id"],
}
TMEET_RECORD_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词"},
        "meeting_code": {"type": "string", "description": "会议号"},
        "start": {"type": "string", "description": "开始时间 ISO 8601"},
        "end": {"type": "string", "description": "结束时间 ISO 8601"},
    },
    "required": [],
}
TMEET_RECORD_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "内容类型 minutes/todos/transcript/paragraphs/timeline"},
        "record_file_id": {"type": "string", "description": "录制文件 ID"},
        "lang": {"type": "string", "description": "语言 zh/en/ja"},
    },
    "required": ["action", "record_file_id"],
}

TMEET = BuiltinConnector(
    key="tmeet",
    kind="cli",
    name="腾讯会议",
    description="通过 tmeet 命令行管理腾讯会议：会议创建/查询、会议纪要、录制转写与待办提取。",
    icon="📹",
    default_config={
        # 项目内 tools/node_modules/.bin 或系统 PATH
        "cli_path": "tmeet",
        "timeout_seconds": 30,
        "tools": [
            # --- 账号认证 ---
            {
                "name": "auth_status",
                "description": "查看腾讯会议账号认证状态（登录用户、Token 有效期）",
                "command": ["auth", "status", "--format", "json"],
                "input_schema": {"type": "object", "properties": {}},
                "risk": "read",
                "approval": "never",
            },
            # --- 会议管理 ---
            {
                "name": "search_meetings",
                "description": "搜索会议（按关键词、会议号、时间范围）",
                "command": [
                    "meeting", "search",
                    "--query", "{query}",
                    "--start", "{start}",
                    "--end", "{end}",
                    "--page-size", "{page_size}",
                    "--format", "json",
                ],
                "input_schema": TMEET_MEETING_SEARCH_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "list_meetings",
                "description": "列出待进行和进行中的会议",
                "command": ["meeting", "list", "--format", "json"],
                "input_schema": {"type": "object", "properties": {}},
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "list_ended_meetings",
                "description": "列出已结束的会议",
                "command": ["meeting", "list-ended", "--format", "json"],
                "input_schema": {"type": "object", "properties": {}},
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "get_meeting",
                "description": "获取会议详情",
                "command": ["meeting", "get", "--meeting-id", "{meeting_id}", "--format", "json"],
                "input_schema": TMEET_MEETING_ID_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "get_meeting_invitees",
                "description": "获取会议参会人列表",
                "command": ["meeting", "invitees-list", "--meeting-id", "{meeting_id}", "--format", "json"],
                "input_schema": TMEET_MEETING_ID_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            # --- 会议纪要 ---
            {
                "name": "search_minutes",
                "description": "搜索指定时间范围内的会议纪要列表",
                "command": [
                    "minutes", "search",
                    "--start", "{start}",
                    "--end", "{end}",
                    "--page-size", "{page_size}",
                    "--format", "json",
                ],
                "input_schema": TMEET_TIME_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "get_minutes",
                "description": "获取会议稳态纪要（概要、要点、待办、主题）",
                "command": ["minutes", "get", "--minute-id", "{minute_id}", "--format", "json"],
                "input_schema": TMEET_MINUTES_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "get_short_summary",
                "description": "获取带时间戳的滚动纪要明细",
                "command": [
                    "minutes", "get", "--short-summary",
                    "--minute-id", "{minute_id}",
                    "--page-size", "{page_size}",
                    "--format", "json",
                ],
                "input_schema": TMEET_SHORT_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            # --- 录制与转写 ---
            {
                "name": "search_records",
                "description": "搜索会议录制文件",
                "command": [
                    "record", "search",
                    "--query", "{query}",
                    "--meeting-code", "{meeting_code}",
                    "--start", "{start}",
                    "--end", "{end}",
                    "--format", "json",
                ],
                "input_schema": TMEET_RECORD_SEARCH_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
            {
                "name": "get_record_content",
                "description": "读取录制内容（智能纪要/待办/转写/段落/时间线）",
                "command": [
                    "record", "content",
                    "--action", "{action}",
                    "--record-file-id", "{record_file_id}",
                    "--lang", "{lang}",
                    "--format", "json",
                ],
                "input_schema": TMEET_RECORD_CONTENT_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
        ],
    },
    config_fields=(
        {
            "key": "cli_path",
            "label": "CLI 路径",
            "field_type": "text",
            "required": True,
            "default": "tmeet",
            "placeholder": "tmeet",
            "is_secret": False,
        },
    ),
)

# Tavily 联网搜索 API：真实可用（需 API Key）
TAVILY_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词"},
        "max_results": {"type": "integer", "default": 5, "description": "返回条数 1-10"},
        "search_depth": {"type": "string", "default": "basic", "description": "搜索深度 basic/advanced"},
    },
    "required": ["query"],
}

TAVILY = BuiltinConnector(
    key="tavily_search",
    kind="api",
    name="Tavily 联网搜索",
    description="通过 Tavily API 搜索互联网获取最新信息，适合实时数据、新闻和技术文档。",
    icon="🔍",
    default_config={
        "base_url": "https://api.tavily.com",
        # Tavily 把 api_key 放在 body 里（不是 header）
        "body_mapping": {"api_key": "api_key"},
        "tools": [
            {
                "name": "search",
                "description": "搜索互联网并返回结构化结果（标题、URL、摘要、相关度）",
                "method": "POST",
                "path": "/search",
                "body_template": {
                    "query": "{query}",
                    "max_results": "{max_results}",
                    "search_depth": "{search_depth}",
                },
                "input_schema": TAVILY_SEARCH_SCHEMA,
                "risk": "read",
                "approval": "never",
            },
        ],
    },
    config_fields=(
        {
            "key": "base_url",
            "label": "API 地址",
            "field_type": "text",
            "required": True,
            "default": "https://api.tavily.com",
            "placeholder": "https://api.tavily.com",
            "is_secret": False,
        },
        {
            "key": "api_key",
            "label": "API Key",
            "field_type": "secret",
            "required": True,
            "default": "",
            "placeholder": "tvly-xxx",
            "is_secret": True,
        },
    ),
)


BUILTIN_CONNECTORS: tuple[BuiltinConnector, ...] = (TMEET, TAVILY)


def get_builtin(key: str) -> BuiltinConnector | None:
    """按标识查找内置连接器模板。

    Args:
        key: 连接器标识。

    Returns:
        内置模板或空。
    """
    for item in BUILTIN_CONNECTORS:
        if item.key == key:
            return item
    return None
