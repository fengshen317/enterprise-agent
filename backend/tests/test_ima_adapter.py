"""核对腾讯 ima 只读接口的请求构造、结果投影和公开异常。"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.connectors import ima
from app.connectors.base import ConnectorError

_CREDENTIALS = {"client_id": "test-client", "api_key": "test-secret"}


@pytest.mark.parametrize(
    ("name", "arguments", "path", "body", "data", "visible"),
    [
        (
            "search_knowledge_base",
            {},
            "wiki/v1/search_knowledge_base",
            {"query": "", "cursor": "", "limit": 20},
            {"info_list": [{"id": "kb", "name": "知识库", "cover_url": "hidden"}], "next_cursor": "n"},
            {"items": [{"id": "kb", "name": "知识库"}], "next_cursor": "n", "truncated": False},
        ),
        (
            "search_knowledge_base",
            {},
            "wiki/v1/search_knowledge_base",
            {"query": "", "cursor": "", "limit": 20},
            {
                "info_list": [
                    {"kb_id": "live-kb", "kb_name": "线上格式", "description": "不返回该字段"},
                    {"id": "legacy-kb", "name": "文档格式"},
                ],
                "next_cursor": "unused",
                "is_end": True,
            },
            {
                "items": [{"id": "live-kb", "name": "线上格式"}, {"id": "legacy-kb", "name": "文档格式"}],
                "is_end": True,
                "truncated": False,
            },
        ),
        (
            "search_knowledge_base",
            {"query": "缺少匹配"},
            "wiki/v1/search_knowledge_base",
            {"query": "缺少匹配", "cursor": "", "limit": 20},
            {"info_list": [], "next_cursor": "", "is_end": True},
            {"items": [], "is_end": True, "truncated": False},
        ),
        (
            "search_knowledge",
            {"knowledge_base_id": "kb", "query": "周报"},
            "wiki/v1/search_knowledge",
            {"knowledge_base_id": "kb", "query": "周报", "cursor": ""},
            {"info_list": [{"media_id": "m", "title": "周报", "highlight_content": "周" * 800}], "is_end": True},
            {
                "items": [{"media_id": "m", "title": "周报", "highlight_content": "周" * 500}],
                "knowledge_base_id": "kb",
                "is_end": True,
                "truncated": False,
            },
        ),
        (
            "get_media_info",
            {"media_id": "m"},
            "wiki/v1/get_media_info",
            {"media_id": "m"},
            {
                "media_type": 11,
                "notebook_ext_info": {"notebook_id": "n"},
                "url_info": {"url": "hidden", "headers": {"Authorization": "hidden"}},
            },
            {"media_id": "m", "media_type": 11, "note_id": "n"},
        ),
        (
            "search_note",
            {"query": "成本", "limit": 2, "start": 3},
            "note/v1/search_note",
            {"search_type": 1, "query_info": {"content": "成本"}, "start": 3, "end": 5},
            {
                "search_note_infos": [{"note_book_info": {"note_id": "n", "title": "成本", "summary": "概况"}}],
                "is_end": False,
            },
            {
                "items": [{"note_id": "n", "title": "成本", "summary": "概况"}],
                "next_start": 5,
                "is_end": False,
                "truncated": False,
            },
        ),
        (
            "search_note",
            {"query": "周报", "by_title": True},
            "note/v1/search_note",
            {"search_type": 0, "query_info": {"title": "周报"}, "start": 0, "end": 20},
            {"is_end": True},
            {"is_end": True, "items": [], "truncated": False},
        ),
        (
            "get_doc_content",
            {"note_id": "n"},
            "note/v1/get_doc_content",
            {"note_id": "n", "target_content_format": 0},
            {"content": "文" * 6001},
            {"note_id": "n", "truncated": True, "content": "文" * 6000},
        ),
    ],
)
async def test_read_contract(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, object],
    path: str,
    body: dict[str, object],
    data: dict[str, object],
    visible: dict[str, object],
) -> None:
    """检查协议路径、当次认证、查询参数和可见字段。

    Args:
        monkeypatch: 测试替换工具。
        name: 工具名称。
        arguments: 输入参数。
        path: 官方路径。
        body: 官方请求体。
        data: 成功响应业务对象。
        visible: 预期结果。
    """
    post = AsyncMock(return_value=httpx.Response(200, json={"code": 0, "data": data}))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await ima.invoke(name, arguments, _CREDENTIALS)
    assert result == {"source": "腾讯 ima", **visible}
    post.assert_awaited_once_with(
        f"https://ima.qq.com/openapi/{path}",
        json=body,
        headers={
            "ima-openapi-clientid": "test-client",
            "ima-openapi-apikey": "test-secret",
            "ima-openapi-ctx": "skill_version=1.1.9",
        },
    )


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401, text="hidden"), "auth_failed"),
        (httpx.Response(403, text="hidden"), "permission_denied"),
        (httpx.Response(302, headers={"location": "https://example.org/hidden"}), "upstream_error"),
        (httpx.Response(200, json={"code": 210005, "msg": "hidden"}), "permission_denied"),
        (httpx.Response(200, json={"code": -1, "msg": "hidden"}), "upstream_error"),
        (httpx.Response(200, text="hidden"), "invalid_output"),
        (httpx.Response(200, json={"code": False, "data": {}}), "invalid_output"),
        (httpx.Response(200, json={"code": 0, "data": {"content": {"hidden": True}}}), "invalid_output"),
    ],
)
async def test_safe_failure(monkeypatch: pytest.MonkeyPatch, response: httpx.Response, code: str) -> None:
    """异常不会带出响应正文、重定向或密钥。

    Args:
        monkeypatch: 测试替换工具。
        response: 失败响应。
        code: 预期分类。
    """
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=response))
    with pytest.raises(ConnectorError) as raised:
        await ima.invoke("get_doc_content", {"note_id": "n"}, _CREDENTIALS)
    assert raised.value.code == code
    assert "hidden" not in str(raised.value)
    assert "test-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("name", "arguments", "credentials", "code"),
    [
        ("import_doc", {}, _CREDENTIALS, "unknown_tool"),
        ("search_knowledge_base", {"limit": 21}, _CREDENTIALS, "invalid_arguments"),
        ("search_note", {"query": " "}, _CREDENTIALS, "invalid_arguments"),
        ("get_media_info", {"media_id": "m", "url": "unknown"}, _CREDENTIALS, "invalid_arguments"),
        ("get_doc_content", {"note_id": "n"}, {}, "auth_failed"),
    ],
)
async def test_reject_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, object],
    credentials: dict[str, str],
    code: str,
) -> None:
    """在发送前拒绝越界工具、无效参数或缺失凭证。

    Args:
        monkeypatch: 测试替换工具。
        name: 工具名称。
        arguments: 工具参数。
        credentials: 测试凭证。
        code: 预期分类。
    """
    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(ConnectorError) as raised:
        await ima.invoke(name, arguments, credentials)
    assert raised.value.code == code
    post.assert_not_awaited()
