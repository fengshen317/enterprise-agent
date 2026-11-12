"""工具输入与公开审计契约。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TOOL_NAME = "get_my_run_stats"


class RunStatsArgs(BaseModel):
    """身份不属于模型参数，额外字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    days: int = Field(default=7, ge=1, le=90, description="统计最近几天，1–90，默认 7")


class RunStatsResult(BaseModel):
    """只返回当前用户的聚合值，不返回消息正文。"""

    days: int
    total: int
    counts: dict[str, int]


class ToolCallView(BaseModel):
    """归属鉴权后的工具执行记录。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    run_id: UUID
    tool_name: str
    tool_version: int | None = None
    risk_level: str | None = None
    approval_required: bool = False
    arguments_digest: str | None = None
    days: int | None
    status: Literal["awaiting_approval", "running", "completed", "failed", "cancelled", "denied", "expired"]
    result: str | None
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None


class ToolView(BaseModel):
    """内置工具目录项，不包含 handler 或内部策略。"""

    key: str
    version: int
    display_name: str
    description: str
    risk: str
    approval: str
    timeout_seconds: float
    quota_per_run: int
    input_schema: dict[str, object]
    enabled: bool = True
    used_by_agents: int = 0


class ToolPolicyInput(BaseModel):
    """空间管理员启停内置工具。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool
