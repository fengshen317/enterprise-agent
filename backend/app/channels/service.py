"""网页侧的微信绑定：取码、轮询确认、列表和解绑。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app.channels.crypto import ChannelCrypto
from app.channels.ilink import ILinkClient, safe_https_base
from app.channels.records import AccountRecord, PeerLink
from app.channels.repository import ChannelStore
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope
from app.schemas.channel import BindSessionView, ChannelAccountView, SkippedPushView


class ChannelService:
    """给已登录用户提供扫码绑定。凭证确认后只以密文入库。"""

    def __init__(self, repo: ChannelStore, ilink: ILinkClient, crypto: ChannelCrypto, settings: Settings) -> None:
        """绑定存储和 iLink 客户端。

        Args:
            repo: 渠道存储。
            ilink: iLink 客户端。
            crypto: token 加密。
            settings: 渠道开关和默认主机。
        """
        self.repo = repo
        self.ilink = ilink
        self.crypto = crypto
        self.settings = settings

    async def start_bind(self, scope: ResourceScope) -> BindSessionView:
        """向 iLink 要一张绑定二维码。

        Args:
            scope: 当前用户和空间。空间会成为绑定后的初始空间。

        Returns:
            待扫码的会话。

        Raises:
            ApplicationError: 渠道未配置，或 iLink 没有返回二维码。
        """
        self._require_channel()
        qr = await self.ilink.get_bot_qrcode(self.settings.ilink_base_url)
        session = await self.repo.create_bind_session(
            scope.user_id,
            scope.workspace_id,
            qr.qrcode,
            qr.img_content,
            safe_https_base(self.settings.ilink_base_url, self.settings.ilink_base_url),
        )
        return BindSessionView(session_id=session.id, status="pending", qrcode_img_content=qr.img_content)

    async def poll_bind(
        self, scope: ResourceScope, session_id: UUID, verify_code: str | None = None
    ) -> BindSessionView:
        """查询扫码进度。确认后创建渠道账号，不把 token 返回给浏览器。

        Args:
            scope: 当前用户和空间。
            session_id: 绑定会话。
            verify_code: 手机上要求输入的数字。

        Returns:
            当前绑定状态。
        """
        self._require_channel()
        session = await self.repo.get_bind_session(scope.user_id, session_id)
        if session.status == "confirmed":
            return BindSessionView(
                session_id=session.id,
                status="confirmed",
                account_id=session.account_id,
                message="微信已绑定",
            )
        if session.status in {"expired", "failed"}:
            return BindSessionView(session_id=session.id, status=session.status, message="绑定已结束，请重新开始")
        code = verify_code if verify_code and verify_code.isdigit() else None
        result = await self.ilink.get_qrcode_status(session.qrcode, session.baseurl, code)
        if result.status == "confirmed":
            return await self._confirm(
                scope.user_id, session.id, result.bot_token, result.ilink_bot_id, result.baseurl, result.ilink_user_id
            )
        if "need_verifycode" in result.status:
            return BindSessionView(
                session_id=session.id,
                status=result.status,
                qrcode_img_content=session.qrcode_img_content,
                message="请输入手机微信上显示的数字",
            )
        if result.status == "scaned_but_redirect" and result.redirect_host:
            baseurl = safe_https_base(_as_origin(result.redirect_host), session.baseurl)
            await self.repo.set_bind_baseurl(session.id, scope.user_id, baseurl)
            return BindSessionView(
                session_id=session.id,
                status="scaned",
                qrcode_img_content=session.qrcode_img_content,
                message="已扫码，正在确认",
            )
        if result.status in {"expired", "verify_code_blocked"}:
            return await self._refresh_or_fail(scope.user_id, session.id, session.refresh_count)
        if result.status == "binded_redirect":
            await self.repo.fail_bind(session.id, scope.user_id, "failed")
            return BindSessionView(session_id=session.id, status="failed", message="这个微信已经绑定过，没有签发新凭证")
        message = "已扫码，请在手机上确认" if result.status == "scaned" else "等待扫码"
        return BindSessionView(
            session_id=session.id,
            status=result.status or "wait",
            qrcode_img_content=session.qrcode_img_content,
            message=message,
        )

    async def list_accounts(self, scope: ResourceScope) -> list[ChannelAccountView]:
        """列出当前用户的微信绑定。

        Args:
            scope: 当前用户。只按用户过滤，不限于当前空间。

        Returns:
            未解绑的账号，不含 token。
        """
        self._require_channel()
        views: list[ChannelAccountView] = []
        for item in await self.repo.list_user_accounts(scope.user_id):
            peer = await self.repo.latest_peer(item.id)
            skipped = await self.repo.list_skipped(item.id)
            views.append(_view(item, peer, skipped, self.settings))
        return views

    async def unbind(self, scope: ResourceScope, account_id: UUID) -> None:
        """解绑自己的微信账号。

        Args:
            scope: 当前用户。
            account_id: 渠道账号。
        """
        self._require_channel()
        await self.repo.unbind(scope.user_id, account_id)

    async def _confirm(
        self, user_id: UUID, session_id: UUID, token: str, bot_id: str, baseurl: str, ilink_user_id: str
    ) -> BindSessionView:
        """加密凭证并落库。

        Args:
            user_id: 当前用户。
            session_id: 绑定会话。
            token: 明文 bot_token。
            bot_id: ilink_bot_id。
            baseurl: 服务端返回的 API 主机。
            ilink_user_id: 扫码用户。

        Returns:
            已确认的绑定视图。

        Raises:
            ApplicationError: 凭证不完整。
        """
        if not token or not ilink_user_id:
            raise ApplicationError("CHANNEL_BIND_FAILED", "微信没有返回可用凭证", 502)
        account = await self.repo.confirm_bind(
            session_id,
            user_id,
            bot_id=bot_id,
            ilink_user_id=ilink_user_id,
            baseurl=safe_https_base(baseurl, self.settings.ilink_base_url),
            ciphertext=self.crypto.encrypt(token),
        )
        return BindSessionView(
            session_id=session_id,
            status="confirmed",
            account_id=account.id,
            ilink_user_id=account.ilink_user_id,
            message="微信已绑定",
        )

    async def _refresh_or_fail(self, user_id: UUID, session_id: UUID, refresh_count: int) -> BindSessionView:
        """过期后最多换三次码。

        Args:
            user_id: 当前用户。
            session_id: 绑定会话。
            refresh_count: 已经换过的次数。

        Returns:
            新的待扫码状态，或过期。
        """
        if refresh_count >= 3:
            await self.repo.fail_bind(session_id, user_id, "expired")
            return BindSessionView(session_id=session_id, status="expired", message="二维码已失效，请重新开始")
        qr = await self.ilink.get_bot_qrcode(self.settings.ilink_base_url)
        session = await self.repo.refresh_bind(session_id, user_id, qr.qrcode, qr.img_content)
        return BindSessionView(
            session_id=session.id,
            status="pending",
            qrcode_img_content=qr.img_content,
            message="二维码已刷新，请重新扫描",
        )

    def _require_channel(self) -> None:
        """渠道关闭时拒绝绑定接口。

        Raises:
            ApplicationError: 未启用或没有密钥。
        """
        if not self.settings.channel_enabled or not self.crypto.configured:
            raise ApplicationError("CHANNEL_NOT_CONFIGURED", "微信渠道未配置密钥", 503)


def _view(
    account: AccountRecord,
    peer: PeerLink | None,
    skipped: list[tuple[datetime, str, str]],
    settings: Settings,
) -> ChannelAccountView:
    """把账号记录转成不含 token 的接口视图，并带上推送窗口。

    Args:
        account: 渠道账号。
        peer: 最近入站的对端。
        skipped: 最近未推送的记录。
        settings: 用来计算窗口和展示时区。

    Returns:
        公开视图。
    """
    until = _window_until(peer, settings.channel_reply_window_hours)
    open_now = until is not None and until > datetime.now(UTC)
    if until is None:
        note = "推送已暂停，还没有收到微信消息"
    elif open_now:
        local = until.astimezone(ZoneInfo(settings.schedule_timezone))
        note = f"推送可用至 {local.strftime('%Y-%m-%d %H:%M')}"
    else:
        note = "推送已暂停"
    return ChannelAccountView(
        id=account.id,
        workspace_id=account.workspace_id,
        status=account.status,
        ilink_bot_id=account.ilink_bot_id,
        ilink_user_id=account.ilink_user_id,
        paused_until=account.paused_until,
        created_at=account.created_at,
        window_open=open_now,
        window_until=until,
        window_note=note,
        skipped_pushes=[
            SkippedPushView(created_at=created, reason=reason, excerpt=excerpt) for created, reason, excerpt in skipped
        ],
    )


def _window_until(peer: PeerLink | None, hours: int) -> datetime | None:
    """计算窗口关闭时间。

    Args:
        peer: 最近入站。
        hours: 窗口小时数。

    Returns:
        关闭时间；还没有入站时为 None。
    """
    if peer is None or peer.last_inbound_at is None:
        return None
    inbound = peer.last_inbound_at
    if inbound.tzinfo is None:
        inbound = inbound.replace(tzinfo=UTC)
    return inbound + timedelta(hours=hours)


def _as_origin(host: str) -> str:
    """补上协议，交给 safe_https_base 再校验。

    Args:
        host: redirect_host，可能已经带协议。

    Returns:
        候选源。
    """
    cleaned = host.strip()
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        return cleaned
    return f"https://{cleaned}"
