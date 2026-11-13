"""审批请求、决策与运行轨迹契约。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "cancelled", "executed", "failed"]


class ApprovalView(BaseModel):
    """用户可见的审批请求；参数预览已脱敏。"""

    id: UUID
    run_id: UUID
    tool_call_id: UUID
    tool_name: str
    tool_version: int
    risk_level: Literal["write", "delete", "external"]
    args_preview: dict[str, object]
    status: ApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decision_comment: str | None


class ApprovalDecisionInput(BaseModel):
    """批准或拒绝一次写操作。"""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=500)


class RunTrace(BaseModel):
    """一次运行的脱敏轨迹，仅运行所有者可读。"""

    run_id: UUID
    agent_snapshot: dict[str, object]
    tool_calls: list[dict[str, object]]
    approvals: list[ApprovalView]
