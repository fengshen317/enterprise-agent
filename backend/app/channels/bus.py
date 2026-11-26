"""按渠道能力把发送交给微信、飞书、钉钉或 QQ。入站处理不直接依赖某一家的客户端。"""

from __future__ import annotations

import logging

from app.channels.capabilities import ChannelCapabilities, capability_for
from app.channels.dingtalk import DingTalkClient
from app.channels.feishu import FeishuClient, approval_card
from app.channels.ilink import ILinkClient
from app.channels.qq_bot import QQBotClient
from app.channels.qq_bot import approval_keyboard as qq_keyboard
from app.channels.records import AccountRecord, AppRecord
from app.channels.repository import ChannelStore
from app.channels.send_error import ChannelSendError

logger = logging.getLogger(__name__)


class ChannelBus:
    """统一的出站口。微信仍走 iLink；企业渠道用应用凭证，而不是用户行上的 token。"""

    def __init__(
        self,
        ilink: ILinkClient,
        feishu: FeishuClient,
        dingtalk: DingTalkClient,
        qq: QQBotClient,
        repo: ChannelStore,
    ) -> None:
        """保存各平台客户端。

        Args:
            ilink: 微信客户端。
            feishu: 飞书客户端。
            dingtalk: 钉钉客户端。
            qq: QQ 机器人客户端。
            repo: 用来读取应用凭证和 QQ 被动序号。
        """
        self.ilink = ilink
        self.feishu = feishu
        self.dingtalk = dingtalk
        self.qq = qq
        self.repo = repo

    def capabilities(self, channel_type: str) -> ChannelCapabilities:
        """给入站处理查询能力标记。

        Args:
            channel_type: 渠道类型。

        Returns:
            能力。
        """
        return capability_for(channel_type)

    async def send_text(self, account: AccountRecord, to_user_id: str, context_token: str, text: str) -> None:
        """发送一段文本。QQ 在被动窗口内带上消息序号，用尽后改为主动消息。

        Args:
            account: 接收绑定。
            to_user_id: 平台用户。
            context_token: 微信会话令牌，或 QQ 被动回复的消息号。
            text: 正文。

        Raises:
            ChannelSendError: 企业渠道拒绝发送。
        """
        if account.channel_type == "ilink":
            await self.ilink.send_text(account, to_user_id, context_token, text)
            return
        app = await self._require_app(account)
        if account.channel_type == "feishu":
            await self.feishu.send_text(app, to_user_id, text)
        elif account.channel_type == "dingtalk":
            await self.dingtalk.send_text(app, to_user_id, text)
        elif account.channel_type == "qq":
            await self._qq_text(app, account, to_user_id, context_token, text)
        else:
            raise ChannelSendError("unsupported_channel", retryable=False)

    async def send_typing(self, account: AccountRecord, to_user_id: str, context_token: str, active: bool) -> None:
        """有输入状态的渠道才发送。QQ 的「正在输入」占用一次被动回复。

        Args:
            account: 接收绑定。
            to_user_id: 平台用户。
            context_token: 微信令牌或 QQ 消息号。
            active: 微信用来开关输入状态。QQ 只在打开时发一次。
        """
        if account.channel_type == "ilink":
            await self.ilink.send_typing(account, to_user_id, context_token, active)
            return
        if account.channel_type != "qq" or not active or not context_token:
            return
        app = await self._require_app(account)
        seq = await self.repo.next_passive_seq(account.id, account.workspace_id, to_user_id, context_token)
        if seq is None:
            return
        try:
            await self.qq.send_typing(app, to_user_id, context_token, seq)
        except ChannelSendError:
            logger.info("qq_typing_skipped", extra={"account_id": str(account.id)})

    async def send_approval(
        self,
        account: AccountRecord,
        to_user_id: str,
        context_token: str,
        text: str,
        *,
        code: str | None,
        web_only: bool,
    ) -> None:
        """能用按钮时附上同意/拒绝。失败则退回纯文本，决策仍走审批服务。

        Args:
            account: 接收绑定。
            to_user_id: 平台用户。
            context_token: 回复上下文。
            text: 说明。
            code: 写操作确认码。
            web_only: 删除和外发只给网页链接。
        """
        if web_only or not code or account.channel_type == "ilink":
            await self.send_text(account, to_user_id, context_token, text)
            return
        try:
            if account.channel_type == "feishu":
                app = await self._require_app(account)
                await self.feishu.send_card(app, to_user_id, approval_card(text, code=code, web_only=False))
                return
            if account.channel_type == "qq":
                app = await self._require_app(account)
                seq = None
                if context_token:
                    seq = await self.repo.next_passive_seq(
                        account.id,
                        account.workspace_id,
                        to_user_id,
                        context_token,
                    )
                await self.qq.send_keyboard(
                    app,
                    to_user_id,
                    qq_keyboard(text, code),
                    msg_id=context_token if seq else "",
                    msg_seq=seq,
                )
                return
        except ChannelSendError:
            logger.info("channel_card_fallback", extra={"channel": account.channel_type})
        await self.send_text(account, to_user_id, context_token, text)

    async def send_app_text(self, app: AppRecord, peer_id: str, text: str) -> None:
        """给尚未绑定的平台用户回绑定说明。

        Args:
            app: 企业应用。
            peer_id: 平台用户。
            text: 说明。

        Raises:
            ChannelSendError: 平台拒绝。
        """
        if app.channel_type == "feishu":
            await self.feishu.send_text(app, peer_id, text)
        elif app.channel_type == "dingtalk":
            await self.dingtalk.send_text(app, peer_id, text)
        elif app.channel_type == "qq":
            await self.qq.send_text(app, peer_id, text)
        else:
            raise ChannelSendError("unsupported_channel", retryable=False)

    async def _qq_text(
        self,
        app: AppRecord,
        account: AccountRecord,
        openid: str,
        msg_id: str,
        text: str,
    ) -> None:
        """被动次数用完后改发主动消息，避免进度文本把 4 次额度占满后最终回答发不出去。"""
        seq = None
        if msg_id:
            seq = await self.repo.next_passive_seq(account.id, account.workspace_id, openid, msg_id)
        if seq is None:
            await self.qq.send_text(app, openid, text)
            return
        await self.qq.send_text(app, openid, text, msg_id=msg_id, msg_seq=seq)

    async def _require_app(self, account: AccountRecord) -> AppRecord:
        """企业绑定必须能解密到应用密钥。"""
        if account.app_id is None:
            raise ChannelSendError("app_missing", retryable=False)
        app = await self.repo.get_app(account.app_id)
        if app is None or not app.secret or app.status != "active":
            raise ChannelSendError("app_missing", retryable=False)
        return app

    async def aclose(self) -> None:
        """关闭企业渠道的 HTTP 客户端。"""
        await self.feishu.aclose()
        await self.dingtalk.aclose()
        await self.qq.aclose()
