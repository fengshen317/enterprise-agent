"""空间成员管理契约，角色保持固定双角色。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

Role = Literal["admin", "member"]
MemberStatus = Literal["active", "revoked"]


class MemberCreate(BaseModel):
    """新账号只能加入当前空间，身份字段不能指定其他空间。"""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    password: SecretStr
    role: Role = "member"

    @field_validator("display_name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        """显示名需可辨认，密码则保持原始输入。

        Args:
            value: 用户显示名。

        Returns:
            去除首尾空白的显示名。
        """
        if not value.strip():
            raise ValueError("显示名不能为空")
        return value.strip()


class MemberUpdate(BaseModel):
    """角色与状态一次更新，不接收全局账号状态或密码。"""

    model_config = ConfigDict(extra="forbid")
    role: Role
    status: MemberStatus


class MemberView(BaseModel):
    """仅公开管理当前空间成员所需的字段。"""

    user_id: UUID
    username: str
    display_name: str
    user_status: str
    role: Role
    status: MemberStatus
    joined_at: datetime
    updated_at: datetime


class MemberPage(BaseModel):
    """稳定分页，搜索范围仅限当前空间。"""

    items: list[MemberView]
    total: int
    offset: int
    limit: int


class AdminAuditView(BaseModel):
    """管理操作快照，不包含凭证。"""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    actor_username: str
    target_username: str
    action: str
    before_role: str | None
    before_status: str | None
    after_role: str
    after_status: str
    created_at: datetime
