"""技能与版本契约；正文只以 Markdown 形式保存。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Visibility = Literal["workspace", "private"]
SkillStatus = Literal["draft", "published", "archived"]
Activation = Literal["always", "on_demand"]
KnowledgeMode = Literal["all_readable", "selected"]


class SkillInput(BaseModel):
    """创建技能或新建草稿版本。"""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    visibility: Visibility = "private"
    content_md: str = Field(min_length=1, max_length=20000)
    tool_keys: list[str] = Field(default_factory=list, max_length=20)
    knowledge_mode: KnowledgeMode = "all_readable"
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=50)
    activation: Activation = "always"
    token_budget: int = Field(default=4000, ge=200, le=20000)
    change_note: str = Field(default="", max_length=500)

    @field_validator("name", "description", "change_note")
    @classmethod
    def clean_text(cls, value: str) -> str:
        """去除首尾空白。

        Args:
            value: 原始文本。

        Returns:
            清洗后的文本。
        """
        return value.strip()

    @field_validator("content_md")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        """拒绝只有空白的技能正文。

        Args:
            value: Markdown 正文。

        Returns:
            原始正文。
        """
        if not value.strip():
            raise ValueError("技能正文不能为空")
        return value

    @field_validator("tool_keys")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        """工具声明去重。

        Args:
            value: 工具标识。

        Returns:
            去重后的标识。
        """
        return list(dict.fromkeys(value))


class SkillPublishInput(BaseModel):
    """发布一个已存在的版本。"""

    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class SkillView(BaseModel):
    """技能摘要或详情；列表请求不返回正文。"""

    id: UUID
    key: str
    name: str
    description: str
    visibility: Visibility
    status: SkillStatus
    current_version: int
    owner_user_id: UUID
    is_owner: bool
    can_manage: bool
    activation: Activation
    tool_keys: list[str]
    knowledge_scope: dict[str, object]
    token_budget: int
    content_md: str | None = None
    created_at: datetime
    updated_at: datetime


class SkillVersionView(BaseModel):
    """不可变版本历史。"""

    id: UUID
    version: int
    content_md: str
    content_sha256: str
    tool_keys: list[str]
    knowledge_scope: dict[str, object]
    activation: Activation
    token_budget: int
    change_note: str
    created_at: datetime
    published_at: datetime | None
