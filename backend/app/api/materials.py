"""个人材料的上传、检索与原件接口。"""

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.auth import ScopeDependency
from app.models.conversation import ChatAttachment
from app.schemas.materials import MAX_MATERIAL_QUERY, MaterialPage, MaterialView
from app.services.chat_attachments import IMAGE_MIME, attachment_form, read_attachments
from app.services.materials import MaterialStore, material_namespace

router = APIRouter(prefix="/api/materials", tags=["我的材料"])


def _store(request: Request) -> MaterialStore:
    """材料浏览和直接上传复用数据库，无需启动问答运行。"""
    return MaterialStore(request.app.state.database.session_factory)


@router.get("", response_model=MaterialPage)
async def list_materials(
    request: Request, scope: ScopeDependency,
    q: Annotated[str, Query(max_length=MAX_MATERIAL_QUERY)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> MaterialPage:
    """列出或搜索当前工作空间的个人材料。"""
    return await _store(request).listing(scope, q, offset, limit)


@router.post("", response_model=list[MaterialView], status_code=201)
async def upload_materials(request: Request, scope: ScopeDependency) -> list[MaterialView]:
    """把上传文件直接归档至个人材料。"""
    async with attachment_form(request) as form:
        files = await read_attachments(form, material_namespace(scope))
    return await _store(request).upload(scope, files)


@router.get("/{material_id}", response_model=MaterialView)
async def material_detail(material_id: UUID, request: Request, scope: ScopeDependency) -> MaterialView:
    """提供材料链接所需的轻量元数据。"""
    page = await _store(request).read_text(scope, material_id, 0)
    return page.material.model_copy(update={"excerpt": page.text})


@router.get("/{material_id}/file")
async def material_file(
    material_id: UUID, request: Request, scope: ScopeDependency, download: bool = False,
) -> Response:
    """按个人材料归属授权下载或预览。"""
    return attachment_response(await _store(request).get(scope, material_id, original=True), download)


def attachment_response(item: ChatAttachment, download: bool) -> Response:
    """只对经验证的栅格图片开放内联预览，其余文件以附件返回。"""
    inline = not download and item.media_type in IMAGE_MIME.values()
    disposition = "inline" if inline else "attachment"
    return Response(
        item.data, media_type=item.media_type if inline else "application/octet-stream",
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(item.filename, safe='')}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox", "Referrer-Policy": "no-referrer",
        },
    )
