"""知识库与文档管理接口，权限始终由服务端绑定。"""

from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.api.auth import ScopeDependency
from app.core.errors import ApplicationError
from app.schemas.knowledge import (
    DocumentParseInput,
    KnowledgeBaseInput,
    KnowledgeBaseView,
    KnowledgeChunkPage,
    KnowledgeDocumentView,
    RetrievalTestInput,
    RetrievalTestResult,
)
from app.services.knowledge import KnowledgeService

router = APIRouter(prefix="/api", tags=["知识库"])
INLINE_MIME = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}


def service(request: Request) -> KnowledgeService:
    """获取应用知识库服务。

    Args:
        request: 当前请求。

    Returns:
        知识库服务。

    Raises:
        ApplicationError: 知识库功能未启用。
    """
    executor: KnowledgeService | None = request.app.state.knowledge_service
    if executor is None:
        raise ApplicationError("KNOWLEDGE_DISABLED", "知识库功能未启用", 503)
    return executor


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseView])
async def list_knowledge_bases(
    request: Request,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[KnowledgeBaseView]:
    """列出当前用户可读的知识库。

    Args:
        request: 当前请求。
        scope: 已验证用户和空间。
        offset: 分页偏移。
        limit: 页面大小。

    Returns:
        空间知识库与自己的私有知识库。
    """
    return await service(request).list_bases(scope, offset, limit)


@router.post("/knowledge-bases", response_model=KnowledgeBaseView, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseInput, request: Request, scope: ScopeDependency
) -> KnowledgeBaseView:
    """创建知识库并创建对应 RAGFlow dataset。

    Args:
        body: 知识库输入。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        已就绪的知识库。
    """
    return await service(request).create(scope, body)


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseView)
async def get_knowledge_base(kb_id: UUID, request: Request, scope: ScopeDependency) -> KnowledgeBaseView:
    """读取知识库详情。

    Args:
        kb_id: 知识库标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        知识库公开视图。
    """
    return await service(request).get(scope, kb_id)


@router.post("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseView)
async def update_knowledge_base(
    kb_id: UUID, body: KnowledgeBaseInput, request: Request, scope: ScopeDependency
) -> KnowledgeBaseView:
    """完整更新知识库与解析设置。

    Args:
        kb_id: 知识库标识。
        body: 知识库输入。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        更新后的知识库。
    """
    return await service(request).update(scope, kb_id, body)


@router.post("/knowledge-bases/{kb_id}/delete", status_code=204)
async def delete_knowledge_base(kb_id: UUID, request: Request, scope: ScopeDependency) -> Response:
    """删除知识库及其 RAGFlow dataset。

    Args:
        kb_id: 知识库标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        空响应。
    """
    await service(request).delete(scope, kb_id)
    return Response(status_code=204)


@router.post("/knowledge-bases/{kb_id}/retrieval-test", response_model=RetrievalTestResult)
async def retrieval_test(
    kb_id: UUID, body: RetrievalTestInput, request: Request, scope: ScopeDependency
) -> RetrievalTestResult:
    """在详情页直接测试检索召回，不经过 Agent 工具。"""
    return await service(request).search_test(scope, kb_id, body)


@router.post("/knowledge-bases/{kb_id}/reparse-documents", response_model=list[KnowledgeDocumentView])
async def reparse_documents(kb_id: UUID, request: Request, scope: ScopeDependency) -> list[KnowledgeDocumentView]:
    """把知识库当前配置批量应用到已有文档并重新解析。"""
    return await service(request).reparse_all(scope, kb_id)


@router.post("/knowledge-bases/{kb_id}/documents", response_model=KnowledgeDocumentView, status_code=201)
async def upload_document(
    kb_id: UUID,
    request: Request,
    scope: ScopeDependency,
    file: Annotated[UploadFile, File(description="待上传文档")],
) -> KnowledgeDocumentView:
    """上传文件，随后写入解析配置并触发解析。

    Args:
        kb_id: 知识库标识。
        request: 当前请求。
        scope: 已验证用户和空间。
        file: multipart 文件。

    Returns:
        进入解析状态的文档。
    """
    executor = service(request)
    content = await file.read(executor.settings.knowledge_max_file_bytes + 1)
    filename = Path(file.filename or "未命名文档").name
    return await executor.upload(
        scope,
        kb_id,
        filename,
        content,
        file.content_type or "application/octet-stream",
    )


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[KnowledgeDocumentView])
async def list_documents(kb_id: UUID, request: Request, scope: ScopeDependency) -> list[KnowledgeDocumentView]:
    """列出知识库文档并刷新解析状态。

    Args:
        kb_id: 知识库标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        文档列表。
    """
    return await service(request).list_documents(scope, kb_id)


@router.get("/knowledge-documents/{document_id}", response_model=KnowledgeDocumentView)
async def get_document(document_id: UUID, request: Request, scope: ScopeDependency) -> KnowledgeDocumentView:
    """读取文档公开状态。

    Args:
        document_id: 文档标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        文档公开视图。
    """
    return await service(request).get_document(scope, document_id)


@router.get("/knowledge-documents/{document_id}/file")
async def download_document_file(document_id: UUID, request: Request, scope: ScopeDependency) -> StreamingResponse:
    """鉴权后流式代理原文件，支持浏览器内联预览或下载。"""
    download = await service(request).open_document_file(scope, document_id)
    mime = download.view.mime.split(";")[0].strip().lower()
    inline = mime in INLINE_MIME or mime == "text/markdown"
    if mime == "text/markdown":
        mime = "text/plain"
    elif not inline:
        mime = "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(download.view.name)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if mime == "text/plain":
        # 文本预览不需要脚本能力；HTML/SVG 已强制下载，这里只做最小加固。
        headers["Content-Security-Policy"] = "sandbox"
    if download.file.content_length is not None:
        headers["Content-Length"] = str(download.file.content_length)
    return StreamingResponse(download.file.stream(), media_type=mime, headers=headers)


@router.get("/knowledge-documents/{document_id}/chunks", response_model=KnowledgeChunkPage)
async def list_document_chunks(
    document_id: UUID,
    request: Request,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeChunkPage:
    """读取文档只读分块列表。"""
    return await service(request).list_document_chunks(scope, document_id, offset, limit)


@router.post("/knowledge-documents/{document_id}/parse", response_model=KnowledgeDocumentView)
async def parse_document(
    document_id: UUID,
    body: DocumentParseInput,
    request: Request,
    scope: ScopeDependency,
) -> KnowledgeDocumentView:
    """用可选覆盖配置重新解析文档。

    Args:
        document_id: 文档标识。
        body: 可选解析覆盖。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        文档公开视图。
    """
    return await service(request).parse(scope, document_id, body.parser_settings)


@router.post("/knowledge-documents/{document_id}/delete", status_code=204)
async def delete_document(document_id: UUID, request: Request, scope: ScopeDependency) -> Response:
    """删除文档及 RAGFlow 索引。

    Args:
        document_id: 文档标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        空响应。
    """
    await service(request).delete_document(scope, document_id)
    return Response(status_code=204)
