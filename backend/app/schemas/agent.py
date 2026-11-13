"""Agent 配置与能力绑定的公开契约。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.tools import ToolView

Visibility = Literal["workspace", "private"]
AgentStatus = Literal["active", "archived"]
Activation = Literal["always", "on_demand"]
KnowledgeMode = Literal["all_readable", "selected"]


class AgentSkillInput(BaseModel):
    """绑定一个已发布技能版本，并指定运行时的激活方式。"""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    activation: Activation = "always"


class AgentInput(BaseModel):
    """创建与完整更新共用字段；身份不在此契约中。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    icon: str = Field(default="≋", max_length=32)
    instructions: str = Field(default="", max_length=8000)
    visibility: Visibility = "private"
    status: AgentStatus = "active"
    model: str = Field(default="", max_length=200)
    allow_planning: bool = True
    allow_delegation: bool = True
    tool_keys: list[str] = Field(default_factory=list, max_length=20)
    skills: list[AgentSkillInput] = Field(default_factory=list, max_length=20)
    knowledge_mode: KnowledgeMode = "all_readable"
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=50)
    starter_prompts: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("name", "description", "icon", "instructions")
    @classmethod
    def clean_text(cls, value: str) -> str:
        """去除首尾空白，但保留指令正文中的换行。

        Args:
            value: 原始文本。

        Returns:
            清洗后的文本。
        """
        cleaned = value.strip()
        if not cleaned and value is not None and len(value.strip()) == 0:
            return cleaned
        return cleaned

    @field_validator("starter_prompts")
    @classmethod
    def clean_prompts(cls, value: list[str]) -> list[str]:
        """拒绝空示例并限制单条长度。

        Args:
            value: 示例问题。

        Returns:
            清洗后的示例。
        """
        result = [item.strip() for item in value if item.strip()]
        if any(len(item) > 200 for item in result):
            raise ValueError("示例问题不能超过 200 字")
        return result

    @field_validator("tool_keys")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        """工具绑定去重并拒绝空标识。

        Args:
            value: 工具标识。

        Returns:
            去重后的标识。
        """
        result = list(dict.fromkeys(value))
        if any(not item.strip() for item in result):
            raise ValueError("工具标识不能为空")
        return result


class AgentUpdate(AgentInput):
    """完整更新并校验版本，避免覆盖他人的并发修改。"""

    expected_version: int = Field(ge=1)


class ToolBindingView(BaseModel):
    """Agent 详情中的工具摘要。"""

    key: str
    display_name: str
    risk: str
    approval: str
    enabled: bool = True


class SkillBindingView(BaseModel):
    """Agent 详情中的技能版本摘要。"""

    id: UUID
    key: str
    name: str
    version: int
    activation: Activation


class AgentView(BaseModel):
    """Agent 公开视图；instructions 仅对有管理权限者返回。"""

    id: UUID
    name: str
    description: str
    icon: str
    visibility: Visibility
    status: AgentStatus
    version: int
    model: str
    owner_user_id: UUID
    is_owner: bool
    can_manage: bool
    instructions: str | None = None
    tools: list[ToolBindingView]
    skills: list[SkillBindingView]
    knowledge_scope: dict[str, object]
    allow_planning: bool
    allow_delegation: bool
    starter_prompts: list[str]
    created_at: datetime
    updated_at: datetime


class AgentPreview(BaseModel):
    """装配后的模型视角，用于管理员调试配置。"""

    system_prompt: str
    tools: list[ToolView]
    skills: list[SkillBindingView]
    knowledge_scope: dict[str, object]
    warnings: list[str] = Field(default_factory=list)
