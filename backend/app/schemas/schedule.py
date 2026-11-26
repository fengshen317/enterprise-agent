"""定时问答接口契约，不向客户端暴露执行身份。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.schedule_calendar import ScheduleCalendar


class ScheduleInput(BaseModel):
    """创建与完整更新共用同一份可编辑字段。"""

    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20000)
    conversation_id: UUID
    cron_expression: str = Field(min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    push_to_wechat: bool = False
    push_channels: list[str] | None = None
    calendar_mode: ScheduleCalendar = "all_days"
    revision: int | None = Field(default=None, ge=1)

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
    push_channels: list[str] = Field(default_factory=list)
    calendar_mode: ScheduleCalendar
    calendar_waiting_year: int | None
    revision: int
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


class ScheduleEnabledInput(BaseModel):
    """只修改启用状态，避免列表操作覆盖较新的配置。"""

    enabled: bool


class CreateScheduledTaskArgs(BaseModel):
    """默认在当前对话运行，可为本人的私有 Agent 新建专用对话。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20000)
    cron_expression: str = Field(min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    push_to_wechat: bool = False
    push_channels: list[str] | None = None
    calendar_mode: ScheduleCalendar = "all_days"
    agent_profile_id: UUID | None = Field(
        default=None,
        strict=False,
        description="可选的本人私有 Agent UUID。指定时新建专用对话，省略时使用当前对话。",
    )


class ListScheduledTasksArgs(BaseModel):
    """可按页读取本人任务，避免遗漏创建较早的任务。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    offset: int = Field(default=0, ge=0)


class GetScheduledTaskArgs(BaseModel):
    """使用列表或用户给出的标识读取本人任务。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    schedule_id: UUID = Field(strict=False)


class UpdateScheduledTaskArgs(GetScheduledTaskArgs):
    """只提交本次明确要修改的字段，并绑定读取时的配置版本。"""

    revision: int = Field(ge=1, description="从 get_scheduled_task 读取的当前配置版本")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    calendar_mode: ScheduleCalendar | None = None
    enabled: bool | None = None
    push_to_wechat: bool | None = None
    push_channels: list[str] | None = None

    def changes(self) -> dict[str, object]:
        """提取显式替换的配置，保留未提交的现有值。"""
        return self.model_dump(exclude={"schedule_id", "revision"}, exclude_none=True)

    @model_validator(mode="after")
    def require_changes(self) -> "UpdateScheduledTaskArgs":
        """拒绝空修改和仅含空白的配置项。"""
        changes = self.changes()
        if not changes or any(isinstance(value, str) and not value.strip() for value in changes.values()):
            raise ValueError("至少需要一个有效的修改字段")
        return self


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
