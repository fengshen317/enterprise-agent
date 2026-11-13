"""联网搜索工具的契约测试。"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.capabilities.builtins.web_search import WebSearchArgs, web_search_spec
from app.core.config import Settings
from app.core.errors import ApplicationError


def test_web_search_spec_registered() -> None:
    """工具声明包含正确的风险和配额。"""
    settings = Settings(_env_file=None, environment="test", tavily_api_key="test-key")
    spec = web_search_spec(settings)
    assert spec.key == "web_search"
    assert spec.risk == "read"
    assert spec.approval == "never"
    assert spec.quota_per_run == 3
    assert spec.timeout_seconds == 10


def test_web_search_args_validation() -> None:
    """参数校验拒绝空查询和越界结果数。"""
    with pytest.raises(ValidationError):
        WebSearchArgs(query="")
    with pytest.raises(ValidationError):
        WebSearchArgs(query="test", max_results=11)
    args = WebSearchArgs(query="test", max_results=5)
    assert args.query == "test"
    assert args.max_results == 5


def test_web_search_not_configured() -> None:
    """未配置 API Key 时明确拒绝。"""
    settings = Settings(_env_file=None, environment="test", tavily_api_key="")
    spec = web_search_spec(settings)
    context = SimpleNamespace(settings=settings)
    with pytest.raises(ApplicationError, match="未配置"):
        asyncio.run(spec.handler(context, WebSearchArgs(query="test")))


def test_web_search_audit_no_content() -> None:
    """审计只保存查询和 URL 摘要，不保存网页正文。"""
    settings = Settings(_env_file=None, environment="test", tavily_api_key="test-key")
    spec = web_search_spec(settings)
    result = {
        "query": "test",
        "results": [{"title": "Page", "url": "https://example.com", "content": "secret content", "score": 0.9}],
    }
    audit = spec.audit(WebSearchArgs(query="test"), result)
    assert audit["result_count"] == 1
    assert "secret content" not in str(audit)
    assert audit["urls"] == ["https://example.com"]
