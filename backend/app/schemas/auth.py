"""登录与工作空间公开契约，禁止序列化密码哈希和会话令牌。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


@dataclass(frozen=True)
class AuthContext:
    """服务端验证得到的会话身份。"""

    user_id: UUID
    session_id: UUID
    username: str
    display_name: str
    expires_at: datetime
    first_admin: bool = False
    must_change_password: bool = False


@dataclass(frozen=True)
class ResourceScope:
    """成员校验后得到的资源范围，不由请求正文或模型绑定身份。"""

    user_id: UUID
    workspace_id: UUID


class LoginRequest(BaseModel):
    """登录输入，口令禁止出现在普通 repr 中。"""

    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


class PasswordChange(BaseModel):
    """本人改密必须提供当前密码，账号始终由会话确定。"""

    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr
    new_password: SecretStr


class WorkspaceView(BaseModel):
    """当前用户可用空间，不泄露其他成员信息。"""

    id: UUID
    slug: str
    name: str
    status: str
    role: str


class UserView(BaseModel):
    """当前登录用户的公开字段。"""

    id: UUID
    username: str
    display_name: str


class SessionView(BaseModel):
    """会话与空间信息，原始会话令牌只通过 HttpOnly Cookie 传递。"""

    user: UserView
    workspaces: list[WorkspaceView]
    expires_at: datetime
    csrf_token: str
    system_admin: bool = False
    must_change_password: bool = False
