"""工作空间个人材料的列表、定位与检索契约。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.chat import ChatAttachmentView

MAX_MATERIAL_QUERY = 120


class MaterialView(ChatAttachmentView):
    """不返回附件正文和原始字节，列表允许展示有限命中片段。"""

    created_at: datetime
    source_conversation_id: UUID | None
    excerpt: str = ""


class MaterialPage(BaseModel):
    """分页结果可直接显示在材料页。"""

    items: list[MaterialView]
    has_more: bool


class SearchMaterialsArgs(BaseModel):
    """检索当前运行用户在当前工作空间的已存材料。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    query: str = Field(min_length=1, max_length=MAX_MATERIAL_QUERY)
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def reject_null_byte(cls, value: str) -> str:
        """避免无效字符进入 PostgreSQL 文本查询。"""
        if "\x00" in value:
            raise ValueError("检索词包含无效字符")
        return value


class ReadMaterialArgs(BaseModel):
    """逐段读取检索命中的材料文字，限制单次返回内容。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    material_id: UUID = Field(strict=False)
    offset: int = Field(default=0, ge=0, le=12_000)


class MaterialText(BaseModel):
    """材料文字和下一段游标。"""

    material: MaterialView
    text: str
    next_offset: int | None
