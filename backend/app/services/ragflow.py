"""RAGFlow 1.0 HTTP 适配层，隔离版本字段差异与外部错误。"""

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.errors import ApplicationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetInfo:
    """创建知识库后需要保存的 RAGFlow 信息。"""

    dataset_id: str
    parser_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentInfo:
    """RAGFlow 文档状态。"""

    document_id: str
    name: str
    status: str
    chunk_count: int = 0
    token_count: int = 0
    parser_config: dict[str, Any] = field(default_factory=dict)
    progress: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    """RAGFlow 检索片段，字段名保留原始语义。"""

    content: str
    dataset_id: str
    document_id: str
    document_name: str
    similarity: float | None = None
    positions: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkInfo:
    """文档分块，仅保留只读浏览所需字段。"""

    content: str
    available: bool = True


@dataclass(frozen=True)
class ChunkPage:
    """分块分页结果。"""

    total: int
    chunks: list[ChunkInfo] = field(default_factory=list)


@dataclass
class DocumentFile:
    """原文件流；响应结束后由迭代器释放底层连接。"""

    content_type: str
    content_length: int | None
    blocks: AsyncGenerator[bytes]

    def stream(self) -> AsyncGenerator[bytes]:
        """返回可交给 StreamingResponse 的字节迭代器。

        Returns:
            原文件字节流。
        """
        return self.blocks

    async def aclose(self) -> None:
        """未消费时显式释放底层连接。"""
        await self.blocks.aclose()


class RagflowClientProtocol(Protocol):
    """知识库服务依赖的最小 RAGFlow 接口，测试可注入替身。"""

    async def create_dataset(
        self, name: str, parser_id: str, parse_type: int, embedding_model: str = "", language: str = ""
    ) -> DatasetInfo:
        """创建 dataset。"""
        ...

    async def update_dataset(
        self,
        dataset_id: str,
        parser_id: str,
        parse_type: int,
        parser_config: dict[str, Any],
        embedding_model: str = "",
    ) -> None:
        """更新 dataset 默认解析配置。"""
        ...

    async def upload_document(self, dataset_id: str, filename: str, content: bytes, content_type: str) -> DocumentInfo:
        """上传文档，不携带解析参数。"""
        ...

    async def patch_document_parser(
        self,
        dataset_id: str,
        document_id: str,
        parser_id: str,
        parse_type: int,
        parser_config: dict[str, Any],
    ) -> None:
        """更新单文档解析配置。"""
        ...

    async def apply_dataset_parser_config(self, dataset_id: str) -> None:
        """把 dataset 配置同步到已有文档，不自动重新解析。"""
        ...

    async def parse_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """触发文档解析。"""
        ...

    async def list_documents(self, dataset_id: str) -> list[DocumentInfo]:
        """读取文档状态。"""
        ...

    async def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """删除文档。"""
        ...

    async def delete_dataset(self, dataset_id: str) -> None:
        """删除 dataset。"""
        ...

    async def download_document(self, dataset_id: str, document_id: str) -> DocumentFile:
        """以流式方式读取文档原文件。"""
        ...

    async def list_chunks(self, dataset_id: str, document_id: str, page: int, size: int) -> ChunkPage:
        """读取文档分块分页。"""
        ...

    async def retrieve(
        self,
        dataset_ids: list[str],
        question: str,
        top_k: int,
        threshold: float,
        document_ids: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """检索允许的 dataset。"""
        ...

    async def aclose(self) -> None:
        """释放 HTTP 连接。"""
        ...


class RagflowClient:
    """RAGFlow 1.0 的服务端客户端，不向调用方透传原始响应。"""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        """保存配置并建立懒连接客户端。

        Args:
            settings: 应用配置，包含 RAGFlow 地址、密钥和超时。
            http_client: 测试可注入的 HTTP 客户端，不发送真实网络请求。
        """
        self.base_url = settings.ragflow_base_url.rstrip("/")
        self.api_key = settings.ragflow_api_key.get_secret_value().strip()
        self.timeout = settings.ragflow_timeout_seconds
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            timeout=httpx.Timeout(self.timeout),
        )

    def _ensure_configured(self) -> None:
        """缺少密钥时明确拒绝，不把空凭证发到外部服务。

        Raises:
            ApplicationError: 未配置 RAGFlow API Key。
        """
        if not self.api_key:
            raise ApplicationError(
                "RAGFLOW_NOT_CONFIGURED",
                "请在 backend/.env 配置 EA_RAGFLOW_API_KEY 后重启后端",
                503,
            )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """统一处理超时、网络错误、非 JSON 和业务错误。

        Args:
            method: HTTP 方法。
            path: RAGFlow API 路径。
            kwargs: httpx 请求参数。

        Returns:
            已确认 code=0 的 JSON 对象。

        Raises:
            ApplicationError: 外部服务不可用、超时或拒绝请求。
        """
        self._ensure_configured()
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("ragflow_timeout", extra={"path": path})
            raise ApplicationError("RAGFLOW_TIMEOUT", "知识库服务响应超时，请稍后重试", 504) from exc
        except httpx.HTTPError as exc:
            logger.warning("ragflow_unavailable", extra={"path": path, "error_type": type(exc).__name__})
            raise ApplicationError("RAGFLOW_UNAVAILABLE", "知识库服务暂时不可用，请稍后重试", 503) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("ragflow_invalid_json", extra={"path": path, "status": response.status_code})
            raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务返回了无法解析的响应", 502) from exc
        if not isinstance(payload, dict):
            raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务返回格式不正确", 502)
        code = payload.get("code")
        if response.status_code >= 400 or (code is not None and code != 0):
            logger.warning(
                "ragflow_rejected",
                extra={
                    "path": path,
                    "status": response.status_code,
                    "code": code,
                    "ragflow_message": str(payload.get("message", ""))[:300],
                },
            )
            raise ApplicationError("RAGFLOW_REJECTED", "知识库服务拒绝了本次操作，请检查配置或文档", 502)
        return payload

    async def create_dataset(
        self, name: str, parser_id: str, parse_type: int, embedding_model: str = "", language: str = ""
    ) -> DatasetInfo:
        """创建 dataset；RAGFlow 1.0 要求 parse_type 与 parser_id。

        Args:
            name: 知识库名称。
            parser_id: 解析器标识。
            parse_type: 解析类型，1 为 BuiltIn。
            embedding_model: 可选 embedding 模型标识。
            language: 可选语言。

        Returns:
            dataset ID 与默认 parser_config。
        """
        body: dict[str, Any] = {"name": name, "parser_id": parser_id, "parse_type": parse_type}
        if embedding_model:
            body["embedding_model"] = embedding_model
        if language:
            body["language"] = language
        payload = await self._request("POST", "/api/v1/datasets", json=body)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务未返回有效 dataset ID", 502)
        config = data.get("parser_config")
        return DatasetInfo(dataset_id=data["id"], parser_config=config if isinstance(config, dict) else {})

    async def update_dataset(
        self,
        dataset_id: str,
        parser_id: str,
        parse_type: int,
        parser_config: dict[str, Any],
        embedding_model: str = "",
    ) -> None:
        """更新 dataset 默认配置。

        Args:
            dataset_id: RAGFlow dataset ID。
            parser_id: 解析器标识。
            parse_type: 解析类型，1 为 BuiltIn。
            parser_config: 完整或可合并的组件配置。
            embedding_model: 可选 embedding 模型。
        """
        body: dict[str, Any] = {
            "parse_type": parse_type,
            "parser_id": parser_id,
            "parser_config": parser_config,
        }
        if embedding_model:
            body["embedding_model"] = embedding_model
        await self._request("PUT", f"/api/v1/datasets/{dataset_id}", json=body)

    async def upload_document(self, dataset_id: str, filename: str, content: bytes, content_type: str) -> DocumentInfo:
        """上传文件；RAGFlow 1.0 的上传接口不接受解析配置。

        Args:
            dataset_id: RAGFlow dataset ID。
            filename: 文件名。
            content: 文件内容。
            content_type: MIME 类型。

        Returns:
            新建文档信息。
        """
        payload = await self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents",
            files={"file": (filename, content, content_type)},
        )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务未返回有效文档 ID", 502)
        item = data[0]
        document_id = item.get("id")
        if not isinstance(document_id, str):
            raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务未返回有效文档 ID", 502)
        parser_config = item.get("parser_config")
        return DocumentInfo(
            document_id=document_id,
            name=str(item.get("name") or filename),
            status=str(item.get("ingestion_status") or item.get("run") or "UNSTART"),
            chunk_count=int(item.get("chunk_count") or 0),
            token_count=int(item.get("token_count") or 0),
            parser_config=parser_config if isinstance(parser_config, dict) else {},
        )

    async def patch_document_parser(
        self,
        dataset_id: str,
        document_id: str,
        parser_id: str,
        parse_type: int,
        parser_config: dict[str, Any],
    ) -> None:
        """用 PATCH 更新单文档解析配置。

        Args:
            dataset_id: RAGFlow dataset ID。
            document_id: RAGFlow document ID。
            parser_id: 解析器标识。
            parse_type: 解析类型。
            parser_config: 完整或局部组件配置。
        """
        await self._request(
            "PATCH",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}",
            json={"parser_id": parser_id, "parse_type": parse_type, "parser_config": parser_config},
        )

    async def apply_dataset_parser_config(self, dataset_id: str) -> None:
        """把 dataset 默认配置同步到已有文档，调用方必须显式 reparse。

        Args:
            dataset_id: RAGFlow dataset ID。
        """
        await self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents/apply-parser-config",
            json={},
        )

    async def parse_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """触发解析。

        Args:
            dataset_id: RAGFlow dataset ID。
            document_ids: 待解析文档。
        """
        await self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/chunks",
            json={"document_ids": document_ids},
        )

    async def list_documents(self, dataset_id: str) -> list[DocumentInfo]:
        """读取 dataset 下全部文档状态。

        Args:
            dataset_id: RAGFlow dataset ID。

        Returns:
            文档状态列表。
        """
        result: list[DocumentInfo] = []
        page = 1
        while True:
            payload = await self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents",
                params={"page": page, "page_size": 100},
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
                raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务未返回有效文档列表", 502)
            docs = data["docs"]
            for item in docs:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                raw_status = item.get("ingestion_status") or item.get("run")
                status = {
                    "0": "UNSTART",
                    "1": "RUNNING",
                    "2": "CANCEL",
                    "3": "COMPLETED",
                    "4": "FAIL",
                }.get(str(raw_status), str(raw_status or "UNKNOWN"))
                error = item.get("progress_msg")
                parser_config = item.get("parser_config")
                result.append(
                    DocumentInfo(
                        document_id=item["id"],
                        name=str(item.get("name") or ""),
                        status=status,
                        chunk_count=int(item.get("chunk_count") or 0),
                        token_count=int(item.get("token_count") or 0),
                        parser_config=parser_config if isinstance(parser_config, dict) else {},
                        progress=float(item.get("progress") or 0),
                        error=str(error) if status == "FAIL" and isinstance(error, str) and error else None,
                    )
                )
            if len(docs) < 100:
                break
            page += 1
        return result

    async def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """删除文档。

        Args:
            dataset_id: RAGFlow dataset ID。
            document_ids: 待删除文档。
        """
        await self._request(
            "DELETE",
            f"/api/v1/datasets/{dataset_id}/documents",
            json={"ids": document_ids},
        )

    async def delete_dataset(self, dataset_id: str) -> None:
        """删除 dataset。

        Args:
            dataset_id: RAGFlow dataset ID。
        """
        await self._request("DELETE", "/api/v1/datasets", json={"ids": [dataset_id]})

    async def download_document(self, dataset_id: str, document_id: str) -> DocumentFile:
        """流式读取 RAGFlow 文档原文件。

        Args:
            dataset_id: RAGFlow dataset ID。
            document_id: RAGFlow document ID。

        Returns:
            带内容类型和长度的流式文件。

        Raises:
            ApplicationError: 未配置、超时、服务不可用或文档不存在。
        """
        self._ensure_configured()
        path = f"/api/v1/datasets/{dataset_id}/documents/{document_id}"
        try:
            response = await self._client.send(self._client.build_request("GET", path), stream=True)
        except httpx.TimeoutException as exc:
            logger.warning("ragflow_timeout", extra={"path": path})
            raise ApplicationError("RAGFLOW_TIMEOUT", "知识库服务响应超时，请稍后重试", 504) from exc
        except httpx.HTTPError as exc:
            logger.warning("ragflow_unavailable", extra={"path": path, "error_type": type(exc).__name__})
            raise ApplicationError("RAGFLOW_UNAVAILABLE", "知识库服务暂时不可用，请稍后重试", 503) from exc
        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            logger.warning("ragflow_download_rejected", extra={"path": path, "status": response.status_code})
            raise ApplicationError("RAGFLOW_REJECTED", "知识库服务未返回该文档的原文件", 502)
        content_type = (response.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
        length_header = response.headers.get("Content-Length")
        content_length = int(length_header) if length_header and length_header.isdigit() else None

        async def blocks() -> AsyncGenerator[bytes]:
            """逐块转发并在结束时关闭上游响应。"""
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return DocumentFile(
            content_type=content_type or "application/octet-stream",
            content_length=content_length,
            blocks=blocks(),
        )

    async def list_chunks(self, dataset_id: str, document_id: str, page: int, size: int) -> ChunkPage:
        """读取文档分块分页。

        Args:
            dataset_id: RAGFlow dataset ID。
            document_id: RAGFlow document ID。
            page: 页码，从 1 开始。
            size: 每页条数。

        Returns:
            分块分页结果。
        """
        payload = await self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
            params={"page": page, "page_size": size},
        )
        data = payload.get("data")
        raw = data.get("chunks") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ApplicationError("RAGFLOW_INVALID_RESPONSE", "知识库服务未返回有效分块列表", 502)
        chunks: list[ChunkInfo] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            content = item.get("content_with_weight") or item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            available = item.get("available")
            if not isinstance(available, bool):
                available = item.get("available_int") != 0
            chunks.append(ChunkInfo(content=content, available=available))
        total = data.get("total") if isinstance(data, dict) else None
        return ChunkPage(total=int(total) if isinstance(total, int) else len(chunks), chunks=chunks)

    async def retrieve(
        self,
        dataset_ids: list[str],
        question: str,
        top_k: int,
        threshold: float,
        document_ids: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """检索指定 dataset，返回规范化片段。

        Args:
            dataset_ids: 允许检索的 dataset。
            question: 查询文本。
            top_k: 返回条数。
            threshold: 相似度阈值。
            document_ids: 可选的文档过滤。

        Returns:
            检索片段。
        """
        body: dict[str, Any] = {
            "question": question,
            "dataset_ids": dataset_ids,
            "top_k": top_k,
            "similarity_threshold": threshold,
            "page": 1,
            "size": top_k,
        }
        if document_ids:
            body["document_ids"] = document_ids
        payload = await self._request("POST", "/api/v1/retrieval", json=body)
        data = payload.get("data")
        chunks = data.get("chunks") if isinstance(data, dict) else None
        if not isinstance(chunks, list):
            return []
        result: list[RetrievedChunk] = []
        for item in chunks:
            if not isinstance(item, dict):
                continue
            content = item.get("content_with_weight") or item.get("content") or item.get("answer") or item.get("chunk")
            if not isinstance(content, str) or not content.strip():
                continue
            document_id = item.get("document_id") or item.get("doc_id")
            dataset_id = item.get("dataset_id") or item.get("kb_id")
            document_name = item.get("document_keyword") or item.get("docnm_kwd") or item.get("document_name")
            if not isinstance(document_id, str) or not isinstance(dataset_id, str):
                continue
            similarity = item.get("similarity")
            positions = item.get("positions")
            result.append(
                RetrievedChunk(
                    content=content,
                    dataset_id=dataset_id,
                    document_id=document_id,
                    document_name=str(document_name or ""),
                    similarity=float(similarity) if isinstance(similarity, (int, float)) else None,
                    positions=list(positions) if isinstance(positions, list) else [],
                )
            )
        return result

    async def aclose(self) -> None:
        """释放 HTTP 连接池。"""
        await self._client.aclose()
