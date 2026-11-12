"""RAGFlow 1.0 适配层的契约与错误映射验证。"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.services.ragflow import RagflowClient


def build(handler: httpx.MockTransport) -> tuple[RagflowClient, httpx.AsyncClient]:
    """构造使用 MockTransport 的客户端。

    Args:
        handler: 请求处理函数。

    Returns:
        RAGFlow 客户端与其底层 HTTP 客户端。
    """
    settings = Settings(_env_file=None, ragflow_api_key="test-key", ragflow_base_url="http://ragflow")
    http = httpx.AsyncClient(transport=handler, base_url="http://ragflow")
    return RagflowClient(settings, http), http


async def test_create_dataset_sends_parse_type_and_parser_id() -> None:
    """创建 dataset 必须使用 1.0 的 parse_type/parser_id 字段。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "data": {"id": "ds-1", "parser_config": {"x": 1}}})

    client, http = build(httpx.MockTransport(handler))
    try:
        info = await client.create_dataset("测试库", "general", 1, "bge-m3", "Chinese")
        assert info.dataset_id == "ds-1"
        assert info.parser_config == {"x": 1}
        assert seen["method"] == "POST"
        assert seen["path"] == "/api/v1/datasets"
        assert seen["body"] == {
            "name": "测试库",
            "parser_id": "general",
            "parse_type": 1,
            "embedding_model": "bge-m3",
            "language": "Chinese",
        }
    finally:
        await client.aclose()
        await http.aclose()


async def test_upload_uses_multipart_and_does_not_send_parser_config() -> None:
    """上传只发送文件，解析参数由后续 PATCH 写入。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"code": 0, "data": [{"id": "doc-1", "name": "a.txt", "ingestion_status": "UNSTART"}]},
        )

    client, http = build(httpx.MockTransport(handler))
    try:
        info = await client.upload_document("ds-1", "a.txt", b"hello", "text/plain")
        assert info.document_id == "doc-1"
        assert seen["method"] == "POST"
        assert seen["path"] == "/api/v1/datasets/ds-1/documents"
        assert b"parser_config" not in seen["body"]
        assert b"file" in seen["body"]
    finally:
        await client.aclose()
        await http.aclose()


async def test_patch_document_parser_uses_patch() -> None:
    """文档解析配置必须 PATCH，不能沿用旧版 PUT。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0})

    client, http = build(httpx.MockTransport(handler))
    try:
        await client.patch_document_parser("ds-1", "doc-1", "general", 1, {"GeneralChunker:SixApplesFall": {}})
        assert seen["method"] == "PATCH"
        assert seen["path"] == "/api/v1/datasets/ds-1/documents/doc-1"
        assert seen["body"] == {
            "parser_id": "general",
            "parse_type": 1,
            "parser_config": {"GeneralChunker:SixApplesFall": {}},
        }
    finally:
        await client.aclose()
        await http.aclose()


async def test_retrieve_normalizes_chunks() -> None:
    """检索结果只保留应用需要的来源字段。"""
    payload = {
        "code": 0,
        "data": {
            "chunks": [
                {
                    "content_with_weight": "制度内容",
                    "dataset_id": "ds-1",
                    "document_id": "doc-1",
                    "document_keyword": "员工手册.pdf",
                    "similarity": 0.8,
                    "positions": [1, 2],
                }
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http = build(httpx.MockTransport(handler))
    try:
        chunks = await client.retrieve(["ds-1"], "年假", 5, 0.2)
        assert len(chunks) == 1
        assert chunks[0].content == "制度内容"
        assert chunks[0].dataset_id == "ds-1"
        assert chunks[0].document_name == "员工手册.pdf"
        assert chunks[0].positions == [1, 2]
    finally:
        await client.aclose()
        await http.aclose()


async def test_business_error_is_mapped_without_leaking_message() -> None:
    """RAGFlow code!=0 映射为稳定错误码，不回显外部正文。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 102, "message": "internal detail"})

    client, http = build(httpx.MockTransport(handler))
    try:
        with pytest.raises(ApplicationError) as caught:
            await client.create_dataset("测试", "general", 1)
        assert caught.value.code == "RAGFLOW_REJECTED"
        assert "internal detail" not in caught.value.message
    finally:
        await client.aclose()
        await http.aclose()


async def test_update_parse_and_delete_paths() -> None:
    """更新、解析、同步配置和删除使用 1.0 的方法与路径。"""
    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": 0})

    client, http = build(httpx.MockTransport(handler))
    try:
        await client.update_dataset("ds-1", "general", 1, {"GeneralChunker:SixApplesFall": {}})
        await client.apply_dataset_parser_config("ds-1")
        await client.parse_documents("ds-1", ["doc-1"])
        await client.delete_documents("ds-1", ["doc-1"])
        await client.delete_dataset("ds-1")
        assert [item[0] for item in requests] == ["PUT", "POST", "POST", "DELETE", "DELETE"]
        assert requests[0][1] == "/api/v1/datasets/ds-1"
        assert requests[1][1] == "/api/v1/datasets/ds-1/documents/apply-parser-config"
        assert requests[2][1] == "/api/v1/datasets/ds-1/chunks"
        assert requests[3][1] == "/api/v1/datasets/ds-1/documents"
        assert requests[4][1] == "/api/v1/datasets"
        assert json.loads(requests[0][2]) == {
            "parse_type": 1,
            "parser_id": "general",
            "parser_config": {"GeneralChunker:SixApplesFall": {}},
        }
        assert json.loads(requests[2][2]) == {"document_ids": ["doc-1"]}
        assert json.loads(requests[3][2]) == {"ids": ["doc-1"]}
        assert json.loads(requests[4][2]) == {"ids": ["ds-1"]}
    finally:
        await client.aclose()
        await http.aclose()


async def test_download_document_streams_original_bytes() -> None:
    """下载接口返回原文件流、内容类型和长度。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            content=b"hello bytes",
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Length": "11"},
        )

    client, http = build(httpx.MockTransport(handler))
    try:
        file = await client.download_document("ds-1", "doc-1")
        assert seen["method"] == "GET"
        assert seen["path"] == "/api/v1/datasets/ds-1/documents/doc-1"
        assert file.content_type == "text/plain"
        assert file.content_length == 11
        assert b"".join([chunk async for chunk in file.stream()]) == b"hello bytes"
    finally:
        await client.aclose()
        await http.aclose()


async def test_download_document_maps_missing_file_to_stable_error() -> None:
    """下载失败只暴露稳定错误码，不抛 httpx 原始异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": 404, "message": "not found"})

    client, http = build(httpx.MockTransport(handler))
    try:
        with pytest.raises(ApplicationError) as caught:
            await client.download_document("ds-1", "doc-missing")
        assert caught.value.code == "RAGFLOW_REJECTED"
    finally:
        await client.aclose()
        await http.aclose()


async def test_list_chunks_uses_page_size_and_normalizes() -> None:
    """分块列表使用实测的 page/page_size 参数并归一化字段。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 12,
                    "chunks": [
                        {"content_with_weight": "第一块", "available": True},
                        {"content": "第二块", "available": False},
                        {"content": "", "available": True},
                    ],
                },
            },
        )

    client, http = build(httpx.MockTransport(handler))
    try:
        page = await client.list_chunks("ds-1", "doc-1", 2, 5)
        assert seen["params"] == {"page": "2", "page_size": "5"}
        assert seen["path"] == "/api/v1/datasets/ds-1/documents/doc-1/chunks"
        assert page.total == 12
        assert [(item.content, item.available) for item in page.chunks] == [
            ("第一块", True),
            ("第二块", False),
        ]
    finally:
        await client.aclose()
        await http.aclose()


async def test_missing_api_key_is_rejected_before_request() -> None:
    """未配置密钥时不发出外部请求。"""
    settings = Settings(_env_file=None, ragflow_api_key="", ragflow_base_url="http://ragflow")
    client = RagflowClient(settings)
    try:
        with pytest.raises(ApplicationError) as caught:
            await client.create_dataset("测试", "general", 1)
        assert caught.value.code == "RAGFLOW_NOT_CONFIGURED"
    finally:
        await client.aclose()
