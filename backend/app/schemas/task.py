"""显式任务、计划与持久化进度的公开契约。"""

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.tools import RunStatsArgs

StepKind = Literal["analysis", "tool", "synthesis"]
RunStatus = Literal[
    "pending",
    "planning",
    "awaiting_confirmation",
    "running",
    "retry_wait",
    "paused",
    "completed",
    "failed",
    "cancelled",
]


class PlanStep(BaseModel):
    """只描述当前注册能力，身份由执行器绑定。"""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")
    title: str = Field(min_length=1, max_length=200)
    kind: StepKind
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    instruction: str = Field(default="", max_length=4000)
    tool_name: Literal["get_my_run_stats"] | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        """拒绝工具冒充和把权限参数夹带进普通步骤。

        Returns:
            校验后的步骤。
        """
        if self.kind == "tool":
            if self.tool_name != "get_my_run_stats":
                raise ValueError("工具步骤必须使用已注册的只读工具")
            RunStatsArgs.model_validate(self.arguments)
        elif self.tool_name is not None or self.arguments:
            raise ValueError("分析和汇总步骤不能携带工具或执行参数")
        if not self.title.strip():
            raise ValueError("步骤标题不能为空")
        return self


class PlanDraft(BaseModel):
    """计划必须是有限且无环的依赖图。"""

    model_config = ConfigDict(extra="forbid")
    steps: list[PlanStep] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        """拒绝重复标识、未知依赖和环。

        Returns:
            已校验的计划。
        """
        keys = {step.key for step in self.steps}
        if len(keys) != len(self.steps):
            raise ValueError("步骤标识不能重复")
        completed: set[str] = set()
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on) or not set(step.depends_on) <= keys:
                raise ValueError("存在重复或未知依赖")
        while len(completed) < len(keys):
            ready = {step.key for step in self.steps if set(step.depends_on) <= completed} - completed
            if not ready:
                raise ValueError("计划依赖不能包含环")
            completed.update(ready)
        return self


class PlanEdit(PlanDraft):
    """草案编辑绑定当前执行和最新版本。"""

    run_id: UUID
    revision: int = Field(ge=1)


class TaskCreate(BaseModel):
    """普通问答不自动创建任务，调用方需显式提交。"""

    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=10000)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    source_conversation_id: UUID | None = None


class PlanConfirm(BaseModel):
    """确认绑定确切计划，避免客户端确认过期草案。"""

    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    revision: int = Field(ge=1)
    digest: str = Field(min_length=64, max_length=64)


class TaskView(BaseModel):
    """任务的稳定身份与归属。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    goal: str
    workspace_id: UUID
    owner_user_id: UUID
    source_conversation_id: UUID | None = None
    created_at: datetime


class TaskRunView(BaseModel):
    """当前执行尝试，等待与租约均为持久化状态。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: RunStatus
    error_code: str | None
    result: str | None
    deadline_at: datetime | None
    next_retry_at: datetime | None
    fencing_token: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    version: int


class PlanView(PlanDraft):
    """不可变计划快照。"""

    revision: int
    digest: str


class TaskStepView(BaseModel):
    """步骤结果，不暴露模型私有思维链。"""

    key: str
    title: str
    kind: StepKind
    depends_on: list[str]
    status: str
    attempt_count: int
    result: str | None
    error_code: str | None
    next_retry_at: datetime | None


class TaskActionSummary(BaseModel):
    """公开动作尝试元数据，不返回原始输入或模型中间正文。"""

    id: UUID
    step_key: str | None
    kind: str
    status: str
    attempt_count: int
    max_attempts: int
    error_code: str | None
    next_retry_at: datetime | None


class TaskSnapshot(BaseModel):
    """可重复覆盖的权威快照。"""

    task: TaskView
    run: TaskRunView
    plan: PlanView | None
    steps: list[TaskStepView]
    actions: list[TaskActionSummary] = Field(default_factory=list)
    last_event_sequence: int


class ActionView(BaseModel):
    """逻辑请求账本，成功结果可直接复用。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: str
    attempt_count: int
    result: str | None
    next_retry_at: datetime | None
    error_code: str | None = None


class TaskEventView(BaseModel):
    """持久化有序事件，重连不触发重新执行。"""

    model_config = ConfigDict(from_attributes=True)
    sequence: int
    event_type: str
    data: dict[str, Any]
    created_at: datetime


class TaskControl(BaseModel):
    """控制操作必须绑定执行与版本，避免迟到请求改变新执行。"""

    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    version: int = Field(ge=1)
