"""通过成员自己的 Tavily Key 执行固定地址的联网查询。"""

from app.capabilities.builtins.web_search import search_tavily
from app.connectors.base import ConnectorError, ConnectorToolDef
from app.connectors.catalog import TAVILY_SEARCH_SCHEMA

TOOLS = (ConnectorToolDef("search", "用我的 Tavily 账号搜索互联网", TAVILY_SEARCH_SCHEMA, "read", "never"),)


async def invoke(name: str, arguments: dict[str, object], credentials: dict[str, str]) -> object:
    """凭证来自当前运行身份，禁止使用管理员或环境中的共享 Key。"""
    count = arguments.get("max_results", 5)
    depth = str(arguments.get("search_depth", "basic"))
    if depth not in {"basic", "advanced"}:
        raise ConnectorError("invalid_arguments", "请选择 basic 或 advanced 搜索深度")
    results = await search_tavily(
        credentials["api_key"], str(arguments["query"]), max(1, min(10, int(str(count)))),
        float(str(arguments.get("_timeout_seconds", 10))), search_depth=depth,
    )
    return {"results": [
        {"title": str(item.get("title", ""))[:200], "url": str(item.get("url", ""))[:500],
         "content": str(item.get("content", ""))[:2000], "score": item.get("score")}
        for item in results
    ]}
