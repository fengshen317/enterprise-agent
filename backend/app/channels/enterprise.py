"""企业机器人的配置和员工绑定码。凭证只以密文入库。"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.channels.capabilities import ENTERPRISE_CHANNEL_TYPES, capability_for
from app.channels.crypto import ChannelCrypto
from app.channels.records import AppRecord
from app.channels.repository import ChannelStore
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope


class ChannelAppInput(BaseModel):
    """管理员保存的企业机器人。secret 为空表示沿用已保存的密钥。"""

    channel_type: str = Field(pattern="^(feishu|dingtalk|qq)$")
    external_app_id: str = Field(min_length=1, max_length=128)
    secret: str = Field(default="", max_length=512)
    robot_code: str = Field(default="", max_length=128)
    sandbox: bool = False


class ChannelAppView(BaseModel):
    """返回给页面的应用状态，不含密钥。"""

    channel_type: str
    external_app_id: str
    status: str
    secret_configured: bool
    robot_code: str = ""
    sandbox: bool = False
    updated_at: datetime | None = None


class BindCodeView(BaseModel):
    """一次性绑定码。"""

    code: str
    expires_at: datetime
    instructions: str


class ChannelBindingView(BaseModel):
    """当前用户在某个企业渠道上的绑定。"""

    bound: bool
    account_id: UUID | None = None
    platform_user_id: str = ""
    status: str = ""
    app_configured: bool = False


class EnterpriseChannelService:
    """空间管理员配置一个企业机器人，员工用绑定码把平台身份对应到自己。"""

    def __init__(self, repo: ChannelStore, crypto: ChannelCrypto) -> None:
        """保存存储和加密器。

        Args:
            repo: 渠道存储。
            crypto: 与微信共用的 Fernet 密钥。
        """
        self.repo = repo
        self.crypto = crypto

    async def list_apps(self, scope: ResourceScope) -> list[ChannelAppView]:
        """列出三种企业渠道的配置状态。密钥不返回。

        Args:
            scope: 当前用户。任何成员都能看到是否已配置。

        Returns:
            飞书、钉钉、QQ 各一条。
        """
        self._require_crypto()
        views: list[ChannelAppView] = []
        for channel_type in ENTERPRISE_CHANNEL_TYPES:
            app = await self.repo.active_app(channel_type)
            views.append(_app_view(channel_type, app))
        del scope
        return views

    async def save_app(self, scope: ResourceScope, body: ChannelAppInput) -> ChannelAppView:
        """保存或替换企业机器人。只有空间管理员可以做。

        Args:
            scope: 当前用户和空间。
            body: 平台应用标识和密钥。

        Returns:
            不含密钥的配置状态。

        Raises:
            ApplicationError: 不是管理员，或没有提供可用密钥。
        """
        self._require_crypto()
        await self._require_admin(scope)
        secret = body.secret.strip()
        current = await self.repo.active_app(body.channel_type)
        if not secret:
            if current is None or not current.secret:
                raise ApplicationError("CHANNEL_SECRET_REQUIRED", "请填写应用密钥", 422)
            secret = current.secret
        payload = json.dumps({"secret": secret, "robot_code": body.robot_code.strip()}, ensure_ascii=False)
        public = {"robot_code": body.robot_code.strip(), "sandbox": body.sandbox}
        saved = await self.repo.save_app(
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            channel_type=body.channel_type,
            external_app_id=body.external_app_id.strip(),
            ciphertext=self.crypto.encrypt(payload),
            public_config=public,
        )
        return _app_view(body.channel_type, saved)

    async def disable_app(self, scope: ResourceScope, channel_type: str) -> None:
        """停用企业机器人。

        Args:
            scope: 当前用户。
            channel_type: feishu、dingtalk 或 qq。
        """
        self._require_known(channel_type)
        await self._require_admin(scope)
        await self.repo.disable_app(channel_type)

    async def issue_code(self, scope: ResourceScope, channel_type: str) -> BindCodeView:
        """给当前用户发一个绑定码。

        Args:
            scope: 当前用户。绑定后的初始空间是现在选中的空间。
            channel_type: 企业渠道。

        Returns:
            绑定码和说明。

        Raises:
            ApplicationError: 管理员还没配置这个机器人。
        """
        self._require_known(channel_type)
        app = await self.repo.active_app(channel_type)
        if app is None:
            raise ApplicationError("CHANNEL_NOT_CONFIGURED", "管理员还没有配置这个机器人", 409)
        issued = await self.repo.issue_bind_code(scope.user_id, scope.workspace_id, app.id)
        label = capability_for(channel_type).label
        return BindCodeView(
            code=issued.code,
            expires_at=issued.expires_at,
            instructions=(
                f"打开{label}里和机器人的单聊，发送 /bind {issued.code} 。绑定码 15 分钟内有效，不要发到群里。"
            ),
        )

    async def binding(self, scope: ResourceScope, channel_type: str) -> ChannelBindingView:
        """查看当前用户是否已经绑定。

        Args:
            scope: 当前用户。
            channel_type: 企业渠道。

        Returns:
            绑定状态。
        """
        self._require_known(channel_type)
        app = await self.repo.active_app(channel_type)
        account = await self.repo.account_for_user(scope.user_id, channel_type)
        if account is None:
            return ChannelBindingView(bound=False, app_configured=app is not None)
        return ChannelBindingView(
            bound=True,
            account_id=account.id,
            platform_user_id=account.ilink_user_id,
            status=account.status,
            app_configured=app is not None,
        )

    async def unbind(self, scope: ResourceScope, channel_type: str) -> None:
        """解除当前用户在该渠道上的绑定。

        Args:
            scope: 当前用户。
            channel_type: 企业渠道。
        """
        self._require_known(channel_type)
        account = await self.repo.account_for_user(scope.user_id, channel_type)
        if account is None:
            raise ApplicationError("CHANNEL_NOT_FOUND", "还没有绑定", 404)
        await self.repo.unbind(scope.user_id, account.id)

    def _require_crypto(self) -> None:
        """没有 Fernet 密钥时不能保存应用凭证。"""
        if not self.crypto.configured:
            raise ApplicationError("CHANNEL_NOT_CONFIGURED", "渠道密钥还没有配置", 503)

    async def _require_admin(self, scope: ResourceScope) -> None:
        """企业凭证属于整个部署，只允许空间管理员写入。"""
        role = await self.repo.workspace_role(scope.user_id, scope.workspace_id)
        if role != "admin":
            raise ApplicationError("ADMIN_REQUIRED", "只有空间管理员可以配置企业机器人", 403)

    @staticmethod
    def _require_known(channel_type: str) -> None:
        """拒绝微信和企业微信从这条接口进入。"""
        if channel_type not in ENTERPRISE_CHANNEL_TYPES:
            raise ApplicationError("CHANNEL_NOT_FOUND", "没有这个企业渠道", 404)


def _app_view(channel_type: str, app: AppRecord | None) -> ChannelAppView:
    """把应用收成页面视图。没有配置时也返回一行，方便目录展示。"""
    if app is None:
        return ChannelAppView(channel_type=channel_type, external_app_id="", status="disabled", secret_configured=False)
    return ChannelAppView(
        channel_type=channel_type,
        external_app_id=app.external_app_id,
        status=app.status,
        secret_configured=bool(app.secret),
        robot_code=app.robot_code,
        sandbox=app.sandbox,
    )
