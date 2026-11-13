"""可持久化摘要与未摘要消息的执行快照。"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ContextMessage:
    """保留序号以追踪摘要实际覆盖的完整问答轮次。"""

    sequence: int
    role: str
    content: str


@dataclass(frozen=True)
class ContextState:
    """领取运行时读取的摘要版本及其后的成功消息。"""

    run_id: UUID
    summary: str
    summary_through_sequence: int
    summary_version: int
    messages: list[ContextMessage]
    model: str = "deepseek-flash"
