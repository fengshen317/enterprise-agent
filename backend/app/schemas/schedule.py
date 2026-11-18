"""定时问答接口契约，不向客户端暴露执行身份。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScheduleInput(BaseModel):
    """创建与完整更新共用同一份可编辑字段。"""

    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20000)
    conversation_id: UUID
    cron_expression: str = Field(min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    push_to_wechat: bool = False

    @field_validator("name", "cron_expression")
    @classmethod
    def clean_short_text(cls, value: str) -> str:
        """去掉名称与表达式的首尾空白。

        Args:
            value: 原始输入。

        Returns:
            非空的标准文本。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("字段不能为空")
        return cleaned

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value: str) -> str:
        """拒绝只有空白的问题，保留正文格式。

        Args:
            value: 原始问题。

        Returns:
            原始正文。
        """
        if not value.strip():
            raise ValueError("定时问题不能为空")
        return value

    @field_validator("timezone")
    @classmethod
    def clean_timezone(cls, value: str | None) -> str | None:
        """归一化可选的时区名称。

        Args:
            value: 客户端输入或空值。

        Returns:
            去空白后的时区名称或空值。
        """
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("时区不能为空")
        return cleaned


class ScheduleView(BaseModel):
    """定时任务列表项，附带固定对话标题便于展示。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    conversation_title: str
    name: str
    prompt: str
    cron_expression: str
    timezone: str
    enabled: bool
    push_to_wechat: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: UUID | None
    last_status: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class ScheduleWeChatPushInput(BaseModel):
    """只修改微信推送开关，不触碰 Cron 的下一次触发时间。"""

    push_to_wechat: bool


class CreateScheduledTaskArgs(BaseModel):
    """模型只能提交任务内容，固定对话由运行上下文绑定。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20000)
    cron_expression: str = Field(min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    push_to_wechat: bool = False


class ListScheduledTasksArgs(BaseModel):
    """列出当前空间定时任务不需要模型提供参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class SetScheduledTaskEnabledArgs(BaseModel):
    """启用或停用一条属于当前用户的定时任务。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    schedule_id: UUID
    enabled: bool

    @field_validator("schedule_id", mode="before")
    @classmethod
    def parse_schedule_id(cls, value: object) -> object:
        """工具协议中的 UUID 来自 JSON 字符串，先显式转换再做严格校验。

        Args:
            value: 模型提交的原始标识。

        Returns:
            UUID 对象或原值，由 Pydantic 继续报告非法格式。
        """
        if isinstance(value, str):
            try:
                return UUID(value)
            except ValueError:
                return value
        return value
