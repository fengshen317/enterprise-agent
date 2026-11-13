"""渠道在服务层传递的记录，避免把 ORM 对象带出事务。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass
class AccountRecord:
    """一个已绑定的 iLink 账号。token 不参与 repr，避免日志泄漏。"""

    id: UUID
    user_id: UUID
    workspace_id: UUID
    channel_type: str
    ilink_bot_id: str
    ilink_user_id: str
    baseurl: str
    updates_cursor: str
    status: str
    paused_until: datetime | None
    created_at: datetime
    welcome_pending: bool = False
    token: str = field(repr=False, default="")


@dataclass(frozen=True)
class PeerLink:
    """某个微信用户在一个空间里对应的对话与最近入站。"""

    account_id: UUID
    workspace_id: UUID
    peer_id: str
    conversation_id: UUID | None
    context_token: str
    last_inbound_at: datetime | None
    reminder_for_inbound_at: datetime | None = None


@dataclass(frozen=True)
class ApprovalTicket:
    """微信短确认码对应的审批。"""

    code: str
    account_id: UUID
    approval_id: UUID
    run_id: UUID
    workspace_id: UUID
    risk_level: str = "write"
    tool_name: str = ""


@dataclass(frozen=True)
class RunDelivery:
    """运行结束后推送到微信所需的正文与归属。"""

    run_id: UUID
    user_id: UUID
    workspace_id: UUID
    status: str
    content: str
    error_message: str | None
    idempotency_key: str


@dataclass
class BindSessionRecord:
    """一次扫码绑定会话。"""

    id: UUID
    user_id: UUID
    workspace_id: UUID
    qrcode: str
    qrcode_img_content: str
    baseurl: str
    status: str
    refresh_count: int
    account_id: UUID | None


@dataclass(frozen=True)
class WorkspaceGate:
    """按 slug 检查成员资格的结果。"""

    found: bool
    allowed: bool
    workspace_id: UUID | None = None
    name: str = ""
