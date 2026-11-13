"""渠道表的短事务。每次调用独立会话，不跨越 iLink 或模型等待。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.commands import new_approval_code
from app.channels.crypto import ChannelCrypto
from app.channels.records import AccountRecord, ApprovalTicket, BindSessionRecord, PeerLink, RunDelivery
from app.core.errors import ApplicationError
from app.models.channel import ChannelAccount, ChannelApprovalCode, ChannelBindSession, ChannelMessage, ChannelPeer
from app.models.conversation import Conversation, Message, Run
from app.services.approvals import ApprovalNotice

logger = logging.getLogger(__name__)


def _approval_risk(risk: str) -> str:
    """只接受审批表允许的风险等级。

    Args:
        risk: 工具声明的风险。

    Returns:
        write、delete 或 external。
    """
    if risk in {"write", "delete", "external"}:
        return risk
    return "write"


class ChannelStore:
    """读写渠道表，并把 bot_token 解密后放进内存记录。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], crypto: ChannelCrypto) -> None:
        """保存会话工厂和加密器。

        Args:
            factory: 应用的数据库会话工厂。
            crypto: bot_token 加解密。
        """
        self.factory = factory
        self.crypto = crypto

    async def get_account(self, account_id: UUID) -> AccountRecord | None:
        """读取账号。

        Args:
            account_id: 渠道账号。

        Returns:
            账号记录；不存在时为 None。
        """
        async with self.factory() as session:
            row = await session.get(ChannelAccount, account_id)
            return self._account(row) if row is not None else None

    async def list_polling_accounts(self) -> list[AccountRecord]:
        """列出仍需长轮询的账号。

        Returns:
            active 和 paused 的账号。
        """
        async with self.factory() as session:
            rows = await session.scalars(select(ChannelAccount).where(ChannelAccount.status.in_(("active", "paused"))))
            return [self._account(row) for row in rows]

    async def list_user_accounts(self, user_id: UUID) -> list[AccountRecord]:
        """列出用户尚未解绑的微信账号。

        Args:
            user_id: 系统用户。

        Returns:
            绑定列表。
        """
        async with self.factory() as session:
            rows = await session.scalars(
                select(ChannelAccount)
                .where(ChannelAccount.user_id == user_id, ChannelAccount.status != "unbound")
                .order_by(ChannelAccount.created_at.desc())
            )
            return [self._account(row) for row in rows]

    async def account_for_user(self, user_id: UUID) -> AccountRecord | None:
        """取用户当前有效绑定。

        Args:
            user_id: 系统用户。

        Returns:
            未解绑账号；没有时为 None。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelAccount).where(
                    ChannelAccount.user_id == user_id,
                    ChannelAccount.channel_type == "ilink",
                    ChannelAccount.status != "unbound",
                )
            )
            return self._account(row) if row is not None else None

    async def mark_active(self, account_id: UUID) -> None:
        """暂停结束后把账号标回可轮询，设置页不再一直显示暂停。

        Args:
            account_id: 渠道账号。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelAccount)
                .where(ChannelAccount.id == account_id, ChannelAccount.status == "paused")
                .values(status="active", paused_until=None, updated_at=datetime.now(UTC))
            )

    async def mark_paused(self, account_id: UUID, until: datetime) -> None:
        """因 errcode -14 暂停该账号的长轮询。

        Args:
            account_id: 渠道账号。
            until: 暂停结束时间。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelAccount)
                .where(ChannelAccount.id == account_id)
                .values(status="paused", paused_until=until, updated_at=datetime.now(UTC))
            )

    async def save_cursor(self, account_id: UUID, cursor: str) -> None:
        """保存非空 get_updates_buf。

        Args:
            account_id: 渠道账号。
            cursor: 下次请求要回传的游标。
        """
        if not cursor:
            return
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelAccount)
                .where(ChannelAccount.id == account_id)
                .values(updates_cursor=cursor, updated_at=datetime.now(UTC))
            )

    async def set_workspace(self, account_id: UUID, workspace_id: UUID) -> None:
        """把后续微信消息切到另一个空间。

        Args:
            account_id: 渠道账号。
            workspace_id: 已通过成员检查的空间。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelAccount)
                .where(ChannelAccount.id == account_id)
                .values(workspace_id=workspace_id, updated_at=datetime.now(UTC))
            )

    async def unbind(self, user_id: UUID, account_id: UUID) -> None:
        """解绑并清掉密文。轮询看到 unbound 后退出。

        Args:
            user_id: 当前用户，防止解绑他人账号。
            account_id: 渠道账号。

        Raises:
            ApplicationError: 绑定不存在。
        """
        async with self.factory.begin() as session:
            row = await session.scalar(
                select(ChannelAccount)
                .where(ChannelAccount.id == account_id, ChannelAccount.user_id == user_id)
                .with_for_update()
            )
            if row is None or row.status == "unbound":
                raise ApplicationError("CHANNEL_NOT_FOUND", "微信绑定不存在", 404)
            row.status = "unbound"
            row.bot_token_ciphertext = ""
            row.updates_cursor = ""
            row.paused_until = None
            row.updated_at = datetime.now(UTC)

    async def record_inbound(
        self,
        account_id: UUID,
        message_id: str,
        peer_id: str,
        status: str,
        reason: str | None,
        excerpt: str,
    ) -> bool:
        """写入入站记录。相同 message_id 冲突时返回 False，用来去重。

        Args:
            account_id: 渠道账号。
            message_id: iLink message_id。空值无法去重，直接返回 False，避免未入库的消息被当成已处理。
            peer_id: 发送者。
            status: received 或 skipped。
            reason: 跳过原因。
            excerpt: 最多 500 字摘录。

        Returns:
            是否第一次看到这条消息。
        """
        if not message_id:
            return False
        stmt = (
            pg_insert(ChannelMessage)
            .values(
                id=uuid4(),
                account_id=account_id,
                peer_id=(peer_id or "unknown")[:128],
                direction="inbound",
                ilink_message_id=message_id[:80],
                client_id="",
                status=status,
                skip_reason=reason,
                body_excerpt=excerpt[:500],
            )
            .on_conflict_do_nothing(constraint="uq_channel_messages_account_message")
            .returning(ChannelMessage.id)
        )
        async with self.factory.begin() as session:
            inserted = await session.scalar(stmt)
        return inserted is not None

    async def touch_inbound(
        self, account_id: UUID, workspace_id: UUID, peer_id: str, context_token: str, at: datetime
    ) -> PeerLink:
        """刷新 24 小时窗口。没有新 token 时保留上一次的 token。

        Args:
            account_id: 渠道账号。
            workspace_id: 当前空间。
            peer_id: 微信用户。
            context_token: 本次入站令牌，可为空。
            at: 入站时间。

        Returns:
            更新后的对端链接。
        """
        async with self.factory.begin() as session:
            row = await self._lock_peer(session, account_id, workspace_id, peer_id)
            row.last_inbound_at = at
            if context_token:
                row.context_token = context_token
            row.updated_at = at
            await session.flush()
            return self._peer(row)

    async def get_peer(self, account_id: UUID, workspace_id: UUID, peer_id: str) -> PeerLink | None:
        """读取某个空间里的对端。

        Args:
            account_id: 渠道账号。
            workspace_id: 空间。
            peer_id: 微信用户。

        Returns:
            对端；没有时为 None。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelPeer).where(
                    ChannelPeer.account_id == account_id,
                    ChannelPeer.workspace_id == workspace_id,
                    ChannelPeer.peer_id == peer_id,
                )
            )
            return self._peer(row) if row is not None else None

    async def set_conversation(
        self, account_id: UUID, workspace_id: UUID, peer_id: str, conversation_id: UUID
    ) -> PeerLink:
        """把对端指向一条对话。

        Args:
            account_id: 渠道账号。
            workspace_id: 空间。
            peer_id: 微信用户。
            conversation_id: 问答对话。

        Returns:
            更新后的链接。
        """
        async with self.factory.begin() as session:
            row = await self._lock_peer(session, account_id, workspace_id, peer_id)
            row.conversation_id = conversation_id
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return self._peer(row)

    async def latest_peer(self, account_id: UUID) -> PeerLink | None:
        """取最近一次入站的对端，用来判断主动推送窗口。

        Args:
            account_id: 渠道账号。

        Returns:
            最近对端；从未入站时可能仍返回空时间的行，或 None。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelPeer)
                .where(ChannelPeer.account_id == account_id)
                .order_by(ChannelPeer.last_inbound_at.desc().nulls_last(), ChannelPeer.id.desc())
                .limit(1)
            )
            return self._peer(row) if row is not None else None

    async def log_outbound(
        self,
        account_id: UUID,
        peer_id: str,
        status: str,
        reason: str | None,
        run_id: UUID | None,
        excerpt: str,
    ) -> None:
        """记录出站或主动推送被跳过的原因。

        Args:
            account_id: 渠道账号。
            peer_id: 接收者，跳过时可能为空。
            status: sent、failed 或 skipped。
            reason: 失败或跳过原因。
            run_id: 关联运行；审批短码通知不占用运行去重。
            excerpt: 短摘录。
        """
        async with self.factory.begin() as session:
            session.add(
                ChannelMessage(
                    account_id=account_id,
                    peer_id=(peer_id or "")[:128],
                    direction="outbound",
                    ilink_message_id=None,
                    client_id=uuid4().hex,
                    run_id=run_id,
                    status=status,
                    skip_reason=reason,
                    body_excerpt=excerpt[:500],
                )
            )

    async def outbound_exists(self, run_id: UUID) -> bool:
        """这个运行是否已经尝试过主动推送。

        Args:
            run_id: 问答运行。

        Returns:
            是否已有出站记录。
        """
        async with self.factory() as session:
            found = await session.scalar(
                select(ChannelMessage.id)
                .where(ChannelMessage.run_id == run_id, ChannelMessage.direction == "outbound")
                .limit(1)
            )
        return found is not None

    async def run_delivery(self, run_id: UUID) -> RunDelivery | None:
        """读取运行归属和助手正文，供主动推送使用。

        Args:
            run_id: 问答运行。

        Returns:
            推送材料；对话没有归属时为 None。
        """
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(
                        Run.status,
                        Run.error_message,
                        Run.idempotency_key,
                        Message.content,
                        Conversation.created_by_user_id,
                        Conversation.workspace_id,
                    )
                    .join(Message, Message.run_id == Run.id)
                    .join(Conversation, Conversation.id == Run.conversation_id)
                    .where(Run.id == run_id, Message.role == "assistant")
                )
            ).one_or_none()
        if row is None or row.created_by_user_id is None or row.workspace_id is None:
            return None
        return RunDelivery(
            run_id=run_id,
            user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
            status=row.status,
            content=row.content or "",
            error_message=row.error_message,
            idempotency_key=row.idempotency_key,
        )

    async def issue_approval_code(self, account_id: UUID, notice: ApprovalNotice) -> str:
        """为待审批写操作签发短码。

        Args:
            account_id: 接收确认码的微信账号。
            notice: 审批摘要。

        Returns:
            四位确认码。

        Raises:
            ApplicationError: 连续冲突后仍无法签发。
        """
        for _ in range(5):
            code = new_approval_code()
            try:
                async with self.factory.begin() as session:
                    session.add(
                        ChannelApprovalCode(
                            code=code,
                            account_id=account_id,
                            approval_id=notice.approval_id,
                            run_id=notice.run_id,
                            workspace_id=notice.workspace_id,
                            expires_at=notice.expires_at,
                            risk_level=_approval_risk(notice.risk_level),
                            tool_name=(notice.tool_name or "")[:64],
                        )
                    )
                return code
            except IntegrityError:
                continue
        raise ApplicationError("CHANNEL_CODE_EXHAUSTED", "暂时无法生成确认码，请在网页处理", 503)

    async def find_approval_code(self, account_id: UUID, code: str) -> ApprovalTicket | None:
        """查找仍有效的确认码。只匹配该微信账号自己的码。

        Args:
            account_id: 渠道账号。
            code: 用户回复的短码。

        Returns:
            审批票据；过期或不存在时为 None。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelApprovalCode).where(
                    ChannelApprovalCode.code == code,
                    ChannelApprovalCode.account_id == account_id,
                    ChannelApprovalCode.status == "pending",
                    ChannelApprovalCode.expires_at > datetime.now(UTC),
                )
            )
            if row is None:
                return None
            return self._ticket(row)

    async def list_pending_codes(self, account_id: UUID) -> list[ApprovalTicket]:
        """列出这个微信账号仍可处理的确认码。

        Args:
            account_id: 渠道账号。

        Returns:
            未过期的待处理确认码。
        """
        async with self.factory() as session:
            rows = await session.scalars(
                select(ChannelApprovalCode)
                .where(
                    ChannelApprovalCode.account_id == account_id,
                    ChannelApprovalCode.status == "pending",
                    ChannelApprovalCode.expires_at > datetime.now(UTC),
                )
                .order_by(ChannelApprovalCode.created_at, ChannelApprovalCode.code)
            )
            return [self._ticket(row) for row in rows]

    async def consume_approval_code(self, account_id: UUID, code: str) -> None:
        """决策完成后标记短码已使用。

        Args:
            account_id: 渠道账号。
            code: 短码。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelApprovalCode)
                .where(ChannelApprovalCode.code == code, ChannelApprovalCode.account_id == account_id)
                .values(status="used")
            )

    async def clear_welcome(self, account_id: UUID) -> None:
        """欢迎语发出后不再重复发送。

        Args:
            account_id: 渠道账号。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelAccount)
                .where(ChannelAccount.id == account_id)
                .values(welcome_pending=False, updated_at=datetime.now(UTC))
            )

    async def mark_reminder(self, account_id: UUID, workspace_id: UUID, peer_id: str, inbound_at: datetime) -> None:
        """记下这条入站对应的窗口已经提醒过，避免同一窗口重复提醒。

        Args:
            account_id: 渠道账号。
            workspace_id: 对端所在空间。
            peer_id: 微信用户。
            inbound_at: 打开当前窗口的入站时间。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelPeer)
                .where(
                    ChannelPeer.account_id == account_id,
                    ChannelPeer.workspace_id == workspace_id,
                    ChannelPeer.peer_id == peer_id,
                )
                .values(reminder_for_inbound_at=inbound_at, updated_at=datetime.now(UTC))
            )

    async def list_skipped(self, account_id: UUID, limit: int = 10) -> list[tuple[datetime, str, str]]:
        """读取最近没能推送出去的出站记录，供网页展示。

        Args:
            account_id: 渠道账号。
            limit: 最多几条。

        Returns:
            时间、原因和摘录。
        """
        async with self.factory() as session:
            rows = await session.execute(
                select(ChannelMessage.created_at, ChannelMessage.skip_reason, ChannelMessage.body_excerpt)
                .where(
                    ChannelMessage.account_id == account_id,
                    ChannelMessage.direction == "outbound",
                    ChannelMessage.status == "skipped",
                )
                .order_by(ChannelMessage.created_at.desc())
                .limit(limit)
            )
            return [(created, reason or "", excerpt) for created, reason, excerpt in rows]

    async def create_bind_session(
        self, user_id: UUID, workspace_id: UUID, qrcode: str, image: str, baseurl: str
    ) -> BindSessionRecord:
        """保存一张待扫的二维码。

        Args:
            user_id: 发起绑定的用户。
            workspace_id: 绑定时的当前空间。
            qrcode: 轮询用的 qrcode。
            image: 展示用的图片地址或内容。
            baseurl: 后续轮询主机。

        Returns:
            绑定会话。
        """
        row = ChannelBindSession(
            user_id=user_id,
            workspace_id=workspace_id,
            qrcode=qrcode,
            qrcode_img_content=image,
            baseurl=baseurl,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        async with self.factory.begin() as session:
            session.add(row)
            await session.flush()
            return self._bind(row)

    async def get_bind_session(self, user_id: UUID, session_id: UUID) -> BindSessionRecord:
        """读取属于该用户的绑定会话。

        Args:
            user_id: 当前用户。
            session_id: 绑定会话。

        Returns:
            绑定会话。

        Raises:
            ApplicationError: 会话不存在。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelBindSession).where(
                    ChannelBindSession.id == session_id, ChannelBindSession.user_id == user_id
                )
            )
            if row is None:
                raise ApplicationError("CHANNEL_BIND_NOT_FOUND", "绑定会话不存在", 404)
            return self._bind(row)

    async def confirm_bind(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        bot_id: str,
        ilink_user_id: str,
        baseurl: str,
        ciphertext: str,
    ) -> AccountRecord:
        """确认扫码。同一用户已有绑定会被替换，避免两张凭证同时轮询。

        Args:
            session_id: 绑定会话。
            user_id: 当前用户。
            bot_id: ilink_bot_id。
            ilink_user_id: 扫码用户。
            baseurl: 后续 API 主机。
            ciphertext: 已加密的 bot_token。

        Returns:
            新的或已经确认过的账号。

        Raises:
            ApplicationError: 会话不存在。
        """
        now = datetime.now(UTC)
        async with self.factory.begin() as session:
            bind = await session.scalar(
                select(ChannelBindSession)
                .where(ChannelBindSession.id == session_id, ChannelBindSession.user_id == user_id)
                .with_for_update()
            )
            if bind is None:
                raise ApplicationError("CHANNEL_BIND_NOT_FOUND", "绑定会话不存在", 404)
            if bind.status == "confirmed" and bind.account_id is not None:
                existing = await session.get(ChannelAccount, bind.account_id)
                if existing is not None:
                    return self._account(existing)
            await session.execute(
                update(ChannelAccount)
                .where(
                    ChannelAccount.user_id == user_id,
                    ChannelAccount.channel_type == "ilink",
                    ChannelAccount.status != "unbound",
                )
                .values(status="unbound", bot_token_ciphertext="", updates_cursor="", updated_at=now)
            )
            account = ChannelAccount(
                user_id=user_id,
                workspace_id=bind.workspace_id,
                ilink_bot_id=bot_id[:128],
                ilink_user_id=ilink_user_id[:128],
                baseurl=baseurl[:300],
                bot_token_ciphertext=ciphertext,
                status="active",
                welcome_pending=True,
            )
            session.add(account)
            await session.flush()
            bind.status = "confirmed"
            bind.account_id = account.id
            bind.updated_at = now
            return self._account(account, self.crypto.decrypt(ciphertext))

    async def refresh_bind(self, session_id: UUID, user_id: UUID, qrcode: str, image: str) -> BindSessionRecord:
        """二维码过期后换一张，最多由调用方限制次数。

        Args:
            session_id: 绑定会话。
            user_id: 当前用户。
            qrcode: 新的 qrcode。
            image: 新的展示内容。

        Returns:
            更新后的会话。

        Raises:
            ApplicationError: 会话不存在。
        """
        async with self.factory.begin() as session:
            row = await self._own_bind(session, session_id, user_id)
            row.qrcode = qrcode
            row.qrcode_img_content = image
            row.refresh_count += 1
            row.status = "pending"
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return self._bind(row)

    async def fail_bind(self, session_id: UUID, user_id: UUID, status: str) -> None:
        """把绑定会话标成过期或失败。

        Args:
            session_id: 绑定会话。
            user_id: 当前用户。
            status: expired 或 failed。
        """
        async with self.factory.begin() as session:
            row = await self._own_bind(session, session_id, user_id)
            row.status = status
            row.updated_at = datetime.now(UTC)

    async def set_bind_baseurl(self, session_id: UUID, user_id: UUID, baseurl: str) -> None:
        """scaned_but_redirect 之后改到新的轮询主机。

        Args:
            session_id: 绑定会话。
            user_id: 当前用户。
            baseurl: 新的 https 源。
        """
        async with self.factory.begin() as session:
            row = await self._own_bind(session, session_id, user_id)
            row.baseurl = baseurl[:300]
            row.updated_at = datetime.now(UTC)

    async def _own_bind(self, session: AsyncSession, session_id: UUID, user_id: UUID) -> ChannelBindSession:
        """锁定并校验绑定会话属于当前用户。

        Args:
            session: 当前事务。
            session_id: 绑定会话。
            user_id: 当前用户。

        Returns:
            绑定行。

        Raises:
            ApplicationError: 会话不存在。
        """
        row = await session.scalar(
            select(ChannelBindSession)
            .where(ChannelBindSession.id == session_id, ChannelBindSession.user_id == user_id)
            .with_for_update()
        )
        if row is None:
            raise ApplicationError("CHANNEL_BIND_NOT_FOUND", "绑定会话不存在", 404)
        return row

    async def _lock_peer(
        self, session: AsyncSession, account_id: UUID, workspace_id: UUID, peer_id: str
    ) -> ChannelPeer:
        """锁定对端行，没有则创建。

        Args:
            session: 当前事务。
            account_id: 渠道账号。
            workspace_id: 空间。
            peer_id: 微信用户。

        Returns:
            对端行。
        """
        row = await session.scalar(
            select(ChannelPeer)
            .where(
                ChannelPeer.account_id == account_id,
                ChannelPeer.workspace_id == workspace_id,
                ChannelPeer.peer_id == peer_id,
            )
            .with_for_update()
        )
        if row is None:
            row = ChannelPeer(account_id=account_id, workspace_id=workspace_id, peer_id=peer_id[:128])
            session.add(row)
            await session.flush()
        return row

    def _account(self, row: ChannelAccount, token: str | None = None) -> AccountRecord:
        """把账号行转成不含密文泄漏的记录。

        Args:
            row: 数据库行。
            token: 已经解密的明文；省略时现场解密。

        Returns:
            账号记录。解密失败时 token 为空，轮询会跳过该账号。
        """
        if token is None:
            token = ""
            if row.bot_token_ciphertext:
                try:
                    token = self.crypto.decrypt(row.bot_token_ciphertext)
                except ApplicationError:
                    logger.warning("channel_token_decrypt_failed", extra={"account_id": str(row.id)})
        return AccountRecord(
            id=row.id,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            channel_type=row.channel_type,
            ilink_bot_id=row.ilink_bot_id,
            ilink_user_id=row.ilink_user_id,
            baseurl=row.baseurl,
            updates_cursor=row.updates_cursor,
            status=row.status,
            paused_until=row.paused_until,
            created_at=row.created_at,
            welcome_pending=row.welcome_pending,
            token=token,
        )

    @staticmethod
    def _peer(row: ChannelPeer) -> PeerLink:
        """转换对端行。

        Args:
            row: 数据库行。

        Returns:
            对端链接。
        """
        return PeerLink(
            account_id=row.account_id,
            workspace_id=row.workspace_id,
            peer_id=row.peer_id,
            conversation_id=row.conversation_id,
            context_token=row.context_token,
            last_inbound_at=row.last_inbound_at,
            reminder_for_inbound_at=row.reminder_for_inbound_at,
        )

    @staticmethod
    def _ticket(row: ChannelApprovalCode) -> ApprovalTicket:
        """转换确认码。

        Args:
            row: 数据库行。

        Returns:
            审批票据。
        """
        return ApprovalTicket(
            row.code,
            row.account_id,
            row.approval_id,
            row.run_id,
            row.workspace_id,
            row.risk_level,
            row.tool_name,
        )

    @staticmethod
    def _bind(row: ChannelBindSession) -> BindSessionRecord:
        """转换绑定会话。

        Args:
            row: 数据库行。

        Returns:
            绑定会话记录。
        """
        return BindSessionRecord(
            id=row.id,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            qrcode=row.qrcode,
            qrcode_img_content=row.qrcode_img_content,
            baseurl=row.baseurl,
            status=row.status,
            refresh_count=row.refresh_count,
            account_id=row.account_id,
        )
