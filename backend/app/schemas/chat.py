"""问答接口契约，禁止返回模型凭证与内部异常。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.approval import ApprovalView

RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class ConversationCreate(BaseModel):
    """创建本地对话的输入。"""

    title: str = Field(default="新对话", min_length=1, max_length=200)
    agent_profile_id: UUID | None = None
    model: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        """拒绝只有空白的标题。

        Args:
            value: 原始标题。

        Returns:
            去除首尾空白的标题。
        """
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()


class RunCreate(BaseModel):
    """重复提交必须复用同一个幂等键。"""

    content: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        """拒绝空问题，保留正文格式。

        Args:
            value: 原始问题。

        Returns:
            合法的原始内容。
        """
        if not value.strip():
            raise ValueError("问题不能为空")
        return value


class ConversationView(BaseModel):
    """对话列表项。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    status: str
    agent_profile_id: UUID | None = None
    model: str | None = None
    created_at: datetime
    updated_at: datetime


class ConversationModelInput(BaseModel):
    """更新对话级模型；空值表示回到 Agent 或全局默认。"""

    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, max_length=200)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str | None) -> str | None:
        """空白模型名视为取消显式选择。

        Args:
            value: 原始模型名。

        Returns:
            去掉空白后的模型名。
        """
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ModelOption(BaseModel):
    """前端可选的模型。"""

    id: str
    max_tokens: int
    thinking: bool
    is_default: bool


class ContextUsageView(BaseModel):
    """上下文占用估算。字符预算是估算，token 仅在供应商返回后填充。"""

    estimated_characters: int = 0
    limit_characters: int = 0
    summary_characters: int = 0
    injected_characters: int = 0
    estimated_tokens: int | None = None
    estimate: Literal[True] = True


class ConversationAgentInput(BaseModel):
    """切换对话使用的 Agent；NULL 表示内置序列助手。"""

    model_config = ConfigDict(extra="forbid")
    agent_profile_id: UUID | None = None


class MessageView(BaseModel):
    """持久化消息。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    run_id: UUID | None
    sequence: int
    role: Literal["user", "assistant"]
    content: str
    reasoning: str | None = None
    model: str | None = None
    status: Literal["streaming", "completed", "interrupted"]
    created_at: datetime
    updated_at: datetime


class RunView(BaseModel):
    """生成尝试的公开状态。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    status: RunStatus
    model: str
    phase: str | None = None
    input_sequence: int
    error_code: str | None
    error_message: str | None
    cancel_requested_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunSnapshot(BaseModel):
    """可重复覆盖的运行快照，用于查询和断线重连。"""

    run: RunView
    message: MessageView
    approval: ApprovalView | None = None
    context_usage: ContextUsageView | None = None


class MessageEdit(BaseModel):
    """编辑最近一条用户消息并重新生成回答。"""

    content: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        """拒绝空白问题。

        Args:
            value: 原始正文。

        Returns:
            保留格式的正文。
        """
        if not value.strip():
            raise ValueError("问题不能为空")
        return value


class RegenerateInput(BaseModel):
    """为最近一条助手消息创建替代运行。"""

    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class MessagePage(BaseModel):
    """按序号递增分页，包含当前未结束运行标识。"""

    items: list[MessageView]
    has_more: bool
    active_run_id: UUID | None
    context_usage: ContextUsageView
