"""让 Agent 在可信运行身份下检索与分段读取个人材料。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.schemas.materials import ReadMaterialArgs, SearchMaterialsArgs
from app.services.materials import MaterialStore

SEARCH_MATERIALS = "search_uploaded_materials"
READ_MATERIAL = "read_uploaded_material"


def material_specs(settings: Settings) -> tuple[ToolSpec, ToolSpec]:
    """注册可在 Agent 装配中启停的两个只读工具。"""

    async def search(context: ToolContext, arguments: BaseModel) -> object:
        """返回本人材料命中片段和可点击的工作台链接。"""
        assert isinstance(arguments, SearchMaterialsArgs)
        page = await MaterialStore(context.factory).listing(context.scope, arguments.query, 0, arguments.limit)
        return {
            "items": [dict(item.model_dump(mode="json"), url=f"/materials/{item.id}") for item in page.items],
            "has_more": page.has_more,
        }

    async def read(context: ToolContext, arguments: BaseModel) -> object:
        """按检索返回的标识读取原文片段。"""
        assert isinstance(arguments, ReadMaterialArgs)
        item = await MaterialStore(context.factory).read_text(context.scope, arguments.material_id, arguments.offset)
        return dict(item.model_dump(mode="json"), url=f"/materials/{item.material.id}")

    def audit_search(arguments: BaseModel, result: object) -> dict[str, object]:
        """工具审计仅记录命中数量，不包含关键词和原文。"""
        hits = result.get("items") if isinstance(result, dict) else None
        return {"count": len(hits) if isinstance(hits, list) else 0}

    def audit_read(arguments: BaseModel, result: object) -> dict[str, object]:
        """读取审计只记录材料标识和起始位置。"""
        assert isinstance(arguments, ReadMaterialArgs)
        return {"material_id": str(arguments.material_id), "offset": arguments.offset}

    return (
        ToolSpec(
            key=SEARCH_MATERIALS, version=1, display_name="检索我的材料",
            description="跨对话搜索本人在当前工作空间上传的材料。按文件名和提取文字查找，请用简短关键词。",
            args_model=SearchMaterialsArgs, risk="read", approval="never",
            timeout_seconds=settings.tool_timeout_seconds,
            quota_per_run=4, handler=search, audit=audit_search,
        ),
        ToolSpec(
            key=READ_MATERIAL, version=1, display_name="读取个人材料",
            description=("用已确认的材料 ID 分段读取提取的文字，按 next_offset 翻页。"
                         "图片可通过文件名定位并提供原图链接。"),
            args_model=ReadMaterialArgs, risk="read", approval="never", timeout_seconds=settings.tool_timeout_seconds,
            quota_per_run=4, handler=read, audit=audit_read,
        ),
    )
