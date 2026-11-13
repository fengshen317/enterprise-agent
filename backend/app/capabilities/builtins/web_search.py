"""联网搜索工具：通过 Tavily API 搜索网页，返回结构化结果。"""

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError

WEB_SEARCH_TOOL = "web_search"
TAVILY_ENDPOINT = "https://api.tavily.com/search"


class WebSearchArgs(BaseModel):
    """联网搜索参数；只读查询，无副作用。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


async def _tavily_search(api_key: str, query: str, max_results: int, timeout_seconds: float) -> list[dict[str, Any]]:
    """调用 Tavily 搜索 API。

    Args:
        api_key: Tavily API Key。
        query: 搜索问题。
        max_results: 返回条数。
        timeout: 超时秒数。

    Returns:
        搜索结果列表。

    Raises:
        ApplicationError: 搜索失败。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                TAVILY_ENDPOINT,
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                },
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return [r for r in results if isinstance(r, dict)]
    except httpx.TimeoutException:
        raise ApplicationError("WEB_SEARCH_TIMEOUT", "联网搜索超时", 504) from None
    except httpx.HTTPStatusError as exc:
        raise ApplicationError("WEB_SEARCH_FAILED", f"搜索服务返回错误 {exc.response.status_code}", 502) from exc
    except Exception as exc:
        raise ApplicationError("WEB_SEARCH_FAILED", "联网搜索失败", 502) from exc


def web_search_spec(settings: Settings) -> ToolSpec:
    """构造联网搜索工具声明。

    Args:
        settings: 超时与返回条数配置。

    Returns:
        只读搜索工具。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """搜索网页并返回结构化结果。

        Args:
            context: 可信运行上下文。
            arguments: 搜索参数。

        Returns:
            搜索结果列表。
        """
        if not isinstance(arguments, WebSearchArgs):
            raise TypeError("搜索参数类型不正确")
        api_key = settings.tavily_api_key.get_secret_value().strip()
        if not api_key:
            raise ApplicationError("WEB_SEARCH_NOT_CONFIGURED", "联网搜索未配置 API Key", 503)
        results = await _tavily_search(
            api_key, arguments.query, arguments.max_results, settings.web_search_timeout_seconds
        )
        # 清洗结果，只保留安全字段。
        clean = []
        for item in results:
            clean.append(
                {
                    "title": str(item.get("title", ""))[:200],
                    "url": str(item.get("url", ""))[:500],
                    "content": str(item.get("content", ""))[:500],
                    "score": item.get("score"),
                }
            )
        return {"query": arguments.query, "results": clean}

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """审计只保存查询和结果摘要，不保存完整网页内容。

        Args:
            arguments: 搜索参数。
            result: 搜索结果。

        Returns:
            脱敏审计摘要。
        """
        assert isinstance(arguments, WebSearchArgs)
        data = result if isinstance(result, dict) else {}
        results = data.get("results", [])
        return {
            "query": arguments.query[:200],
            "result_count": len(results) if isinstance(results, list) else 0,
            "urls": [r.get("url", "") for r in results[:5] if isinstance(r, dict)],
        }

    return ToolSpec(
        key=WEB_SEARCH_TOOL,
        version=1,
        display_name="联网搜索",
        description="搜索互联网获取最新信息。适合需要实时数据、新闻、技术文档或公开资料的问题。",
        args_model=WebSearchArgs,
        risk="read",
        approval="never",
        timeout_seconds=settings.web_search_timeout_seconds,
        quota_per_run=3,
        handler=handler,
        audit=audit,
    )
