"""微信渠道接口契约。不返回 bot_token。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class BindSessionView(BaseModel):
    """扫码绑定进度。"""

    session_id: UUID
    status: str
    qrcode_img_content: str = ""
    account_id: UUID | None = None
    ilink_user_id: str = ""
    message: str = ""


class SkippedPushView(BaseModel):
    """一条没有推送到微信的出站记录。"""

    created_at: datetime
    reason: str
    excerpt: str


class ChannelAccountView(BaseModel):
    """已绑定的微信账号。"""

    id: UUID
    workspace_id: UUID
    status: str
    ilink_bot_id: str = Field(default="")
    ilink_user_id: str = Field(default="")
    paused_until: datetime | None = None
    created_at: datetime
    window_open: bool = False
    window_until: datetime | None = None
    window_note: str = "推送已暂停"
    skipped_pushes: list[SkippedPushView] = Field(default_factory=list)
