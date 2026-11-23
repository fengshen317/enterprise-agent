"""系统日志查询参数与安全响应。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

type Component = Literal["http", "run", "tool", "model", "connector", "schedule", "wechat"]
type Outcome = Literal["success", "warning", "error"]
type Level = Literal["info", "warning", "error"]
type Days = Literal[1, 7, 30]


class SystemLogFilters(BaseModel):
    """限制查询范围和请求大小，防止扫全表和超大响应。"""

    days: Days = 1
    component: Component | None = None
    outcome: Outcome | None = None
    level: Level | None = None
    q: str | None = Field(default=None, max_length=64)
    cursor: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=50, ge=1, le=100)
    connector_key: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("days", mode="before")
    @classmethod
    def parse_days(cls, value: object) -> object:
        """将 HTTP 查询字符串转为受限的天数枚举。

        Args:
            value: 尚未校验的查询值。

        Returns:
            归一化天数，其他输入由模型拒绝。
        """
        return int(value) if isinstance(value, str) and value in {"1", "7", "30"} else value


class SystemLogView(BaseModel):
    """仅公开管理员可见的结构化事件字段。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    occurred_at: datetime
    level: Level
    component: Component
    event: str
    outcome: Outcome
    error_code: str | None
    duration_ms: int | None
    request_id: str | None
    run_id: UUID | None
    tool_call_id: UUID | None
    connector_key: str | None
    tool_name: str | None
    task_execution_id: UUID | None
    details: dict[str, str | int]


class SystemLogCounts(BaseModel):
    """忽略当前结果筛选的各类别数量。"""

    all: int
    success: int
    warning: int
    error: int


class SystemLogPage(BaseModel):
    """当前过滤条件下的一页诊断事件。"""

    items: list[SystemLogView]
    total: int
    counts: SystemLogCounts
    next_cursor: str | None


class SystemLogDetail(BaseModel):
    """诊断事件及按时间排列的其他关联事件。"""

    event: SystemLogView
    related: list[SystemLogView]
    related_truncated: bool
