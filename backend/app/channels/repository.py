"""渠道表的短事务。每次调用独立会话，不跨越 iLink 或模型等待。"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.commands import new_approval_code, new_bind_code
from app.channels.crypto import ChannelCrypto
from app.channels.records import (
    AccountRecord,
    AppRecord,
    ApprovalTicket,
    BindCodeRecord,
    BindSessionRecord,
    PeerLink,
    RunDelivery,
)
from app.core.errors import ApplicationError
from app.models.channel import (
    ChannelAccount,
    ChannelApp,
    ChannelAppEvent,
    ChannelApprovalCode,
    ChannelBindCode,
    ChannelBindSession,
    ChannelMessage,
    ChannelPeer,
)
from app.models.conversation import Conversation, Message, Run
from app.models.schedule import ScheduledTask
from app.services.approvals import ApprovalNotice

logger = logging.getLogger(__name__)


def _ledger_id(message_id: str, limit: int = 80) -> str:
    """平台消息号超长时改存摘要，避免列宽截断后撞车。

    Args:
        message_id: 原始消息号。
        limit: 列宽。

    Returns:
        可入库的标识。
    """
    if len(message_id) <= limit:
        return message_id
    return hashlib.sha256(message_id.encode()).hexdigest()


def _push_channels(raw: object, push_to_wechat: bool) -> tuple[str, ...]:
    """旧行只有微信开关时，把它看成仅含 ilink 的列表。

    Args:
        raw: JSON 列。
        push_to_wechat: 历史布尔开关。

    Returns:
        渠道类型元组。
    """
    if isinstance(raw, list):
        values = tuple(item for item in raw if isinstance(item, str))
        if values or not push_to_wechat:
            return values
    if push_to_wechat:
        return ("ilink",)
    return ()


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
            rows = await session.scalars(
                select(ChannelAccount).where(
                    ChannelAccount.channel_type == "ilink",
                    ChannelAccount.status.in_(("active", "paused")),
                )
            )
            return [self._account(row) for row in rows]

    async def list_user_accounts(self, user_id: UUID, channel_type: str | None = None) -> list[AccountRecord]:
        """列出用户尚未解绑的渠道账号。

        Args:
            user_id: 系统用户。
            channel_type: 只看一种渠道；为空时返回全部。

        Returns:
            绑定列表。
        """
        async with self.factory() as session:
            query = select(ChannelAccount).where(
                ChannelAccount.user_id == user_id,
                ChannelAccount.status != "unbound",
            )
            if channel_type is not None:
                query = query.where(ChannelAccount.channel_type == channel_type)
            rows = await session.scalars(query.order_by(ChannelAccount.created_at.desc()))
            return [self._account(row) for row in rows]

    async def account_for_user(self, user_id: UUID, channel_type: str = "ilink") -> AccountRecord | None:
        """取用户在某个渠道上的当前有效绑定。

        Args:
            user_id: 系统用户。
            channel_type: 渠道类型，默认仍是微信。

        Returns:
            未解绑账号；没有时为 None。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelAccount).where(
                    ChannelAccount.user_id == user_id,
                    ChannelAccount.channel_type == channel_type,
                    ChannelAccount.status != "unbound",
                )
            )
            return self._account(row) if row is not None else None

    async def accounts_for_user(self, user_id: UUID) -> list[AccountRecord]:
        """列出用户全部未解绑渠道，供审批和定时结果逐个投递。

        Args:
            user_id: 系统用户。

        Returns:
            绑定列表。
        """
        return await self.list_user_accounts(user_id)

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
                ilink_message_id=_ledger_id(message_id),
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

    async def activate_conversation(
        self, account_id: UUID, workspace_id: UUID, peer_id: str, conversation_id: UUID
    ) -> None:
        """把微信后续入站原子切到主动推送的来源对话。

        Args:
            account_id: 渠道账号。
            workspace_id: 来源对话所属空间。
            peer_id: 微信用户。
            conversation_id: 来源对话。
        """
        async with self.factory.begin() as session:
            account = await session.scalar(
                select(ChannelAccount).where(ChannelAccount.id == account_id).with_for_update()
            )
            if account is None or account.status == "unbound":
                return
            row = await self._lock_peer(session, account_id, workspace_id, peer_id)
            account.workspace_id = workspace_id
            account.updated_at = datetime.now(UTC)
            row.conversation_id = conversation_id
            row.updated_at = datetime.now(UTC)

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

    async def outbound_exists(self, run_id: UUID, account_id: UUID | None = None) -> bool:
        """这个运行是否已经对某个绑定尝试过主动推送。

        Args:
            run_id: 问答运行。
            account_id: 渠道绑定。为空时只要任一绑定有出站记录即视为已发送。

        Returns:
            是否已有出站记录。
        """
        async with self.factory() as session:
            query = select(ChannelMessage.id).where(
                ChannelMessage.run_id == run_id,
                ChannelMessage.direction == "outbound",
            )
            if account_id is not None:
                query = query.where(ChannelMessage.account_id == account_id)
            found = await session.scalar(query.limit(1))
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
                        Run.conversation_id,
                        Message.content,
                        Conversation.created_by_user_id,
                        Conversation.workspace_id,
                        ScheduledTask.id.label("schedule_id"),
                        ScheduledTask.name.label("schedule_name"),
                        ScheduledTask.push_to_wechat,
                        ScheduledTask.push_channels,
                    )
                    .join(Message, Message.run_id == Run.id)
                    .join(Conversation, Conversation.id == Run.conversation_id)
                    .outerjoin(ScheduledTask, ScheduledTask.last_run_id == Run.id)
                    .where(Run.id == run_id, Message.role == "assistant")
                )
            ).one_or_none()
        if row is None or row.created_by_user_id is None or row.workspace_id is None:
            return None
        return RunDelivery(
            run_id=run_id,
            user_id=row.created_by_user_id,
            workspace_id=row.workspace_id,
            conversation_id=row.conversation_id,
            status=row.status,
            content=row.content or "",
            error_message=row.error_message,
            idempotency_key=row.idempotency_key,
            schedule_id=row.schedule_id,
            schedule_name=row.schedule_name or "",
            push_to_wechat=bool(row.push_to_wechat),
            push_channels=_push_channels(row.push_channels, bool(row.push_to_wechat)),
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

    async def list_active_apps(self) -> list[AppRecord]:
        """列出仍需维持长连接的企业机器人。

        Returns:
            状态为 active 的应用。
        """
        async with self.factory() as session:
            rows = await session.scalars(select(ChannelApp).where(ChannelApp.status == "active"))
            return [self._app(row) for row in rows]

    async def get_app(self, app_id: UUID) -> AppRecord | None:
        """按主键读取企业应用。

        Args:
            app_id: 应用标识。

        Returns:
            应用；不存在时为 None。
        """
        async with self.factory() as session:
            row = await session.get(ChannelApp, app_id)
            return self._app(row) if row is not None else None

    async def active_app(self, channel_type: str) -> AppRecord | None:
        """读取某个企业渠道当前启用的应用。

        Args:
            channel_type: feishu、dingtalk 或 qq。

        Returns:
            应用；没有启用的应用时为 None。
        """
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelApp).where(ChannelApp.channel_type == channel_type, ChannelApp.status == "active")
            )
            return self._app(row) if row is not None else None

    async def save_app(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        channel_type: str,
        external_app_id: str,
        ciphertext: str,
        public_config: dict[str, object],
    ) -> AppRecord:
        """创建或替换该渠道唯一的启用应用。旧的启用记录改为停用。

        Args:
            workspace_id: 配置时所在空间，只用于审计。
            user_id: 操作的管理员。
            channel_type: 企业渠道。
            external_app_id: 平台上的应用标识。
            ciphertext: 已加密的密钥 JSON。
            public_config: 不含密钥的展示字段。

        Returns:
            保存后的应用。
        """
        async with self.factory.begin() as session:
            current = await session.scalar(
                select(ChannelApp)
                .where(ChannelApp.channel_type == channel_type, ChannelApp.status == "active")
                .with_for_update()
            )
            now = datetime.now(UTC)
            if current is None:
                current = ChannelApp(
                    workspace_id=workspace_id,
                    channel_type=channel_type,
                    created_by_user_id=user_id,
                )
                session.add(current)
            current.workspace_id = workspace_id
            current.created_by_user_id = user_id
            current.external_app_id = external_app_id[:128]
            current.credentials_ciphertext = ciphertext
            current.public_config = public_config
            current.status = "active"
            current.updated_at = now
            await session.flush()
            return self._app(current)

    async def disable_app(self, channel_type: str) -> None:
        """停用企业机器人。已绑定的员工保留，但不再收消息。

        Args:
            channel_type: 企业渠道。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelApp)
                .where(ChannelApp.channel_type == channel_type, ChannelApp.status == "active")
                .values(status="disabled", updated_at=datetime.now(UTC))
            )

    async def issue_bind_code(self, user_id: UUID, workspace_id: UUID, app_id: UUID) -> BindCodeRecord:
        """作废该用户尚未使用的绑定码，再发一个 15 分钟内有效的新码。

        Args:
            user_id: 工作台用户。
            workspace_id: 绑定后的初始空间。
            app_id: 企业应用。

        Returns:
            新的绑定码。
        """
        expires = datetime.now(UTC) + timedelta(minutes=15)
        async with self.factory.begin() as session:
            await session.execute(
                update(ChannelBindCode)
                .where(
                    ChannelBindCode.user_id == user_id,
                    ChannelBindCode.app_id == app_id,
                    ChannelBindCode.status == "pending",
                )
                .values(status="expired")
            )
            row = ChannelBindCode(
                user_id=user_id,
                workspace_id=workspace_id,
                app_id=app_id,
                code=new_bind_code(),
                expires_at=expires,
            )
            session.add(row)
            await session.flush()
            return BindCodeRecord(row.id, row.user_id, row.workspace_id, row.app_id, row.code, row.expires_at)

    async def redeem_bind_code(self, channel_type: str, peer_id: str, code: str) -> AccountRecord | None:
        """用单聊里的绑定码建立平台用户和工作台用户的对应关系。

        Args:
            channel_type: 消息来自的企业渠道。
            peer_id: 平台用户标识。
            code: 用户发送的绑定码。

        Returns:
            新的或已经属于同一人的绑定。码无效时为 None。

        Raises:
            ApplicationError: 这个平台账号已经绑给了别人。
        """
        normalized = code.strip().upper()
        if not normalized or not peer_id:
            return None
        async with self.factory.begin() as session:
            row = await session.scalar(
                select(ChannelBindCode)
                .where(ChannelBindCode.code == normalized, ChannelBindCode.status == "pending")
                .with_for_update()
            )
            now = datetime.now(UTC)
            if row is None or row.expires_at < now:
                if row is not None and row.expires_at < now:
                    row.status = "expired"
                return None
            app = await session.get(ChannelApp, row.app_id)
            if app is None or app.status != "active" or app.channel_type != channel_type:
                return None
            taken = await session.scalar(
                select(ChannelAccount)
                .where(
                    ChannelAccount.channel_type == channel_type,
                    ChannelAccount.ilink_user_id == peer_id[:128],
                    ChannelAccount.status != "unbound",
                )
                .with_for_update()
            )
            if taken is not None and taken.user_id != row.user_id:
                raise ApplicationError("CHANNEL_ALREADY_BOUND", "这个账号已经绑定了其他成员", 409)
            previous = await session.scalar(
                select(ChannelAccount)
                .where(
                    ChannelAccount.user_id == row.user_id,
                    ChannelAccount.channel_type == channel_type,
                    ChannelAccount.status != "unbound",
                )
                .with_for_update()
            )
            if previous is not None and previous is not taken:
                previous.status = "unbound"
                previous.updated_at = now
            if taken is None:
                taken = ChannelAccount(
                    user_id=row.user_id,
                    workspace_id=row.workspace_id,
                    channel_type=channel_type,
                    ilink_user_id=peer_id[:128],
                    ilink_bot_id=app.external_app_id[:128],
                    app_id=app.id,
                    baseurl="",
                    status="active",
                    welcome_pending=True,
                )
                session.add(taken)
            else:
                taken.app_id = app.id
                taken.workspace_id = row.workspace_id
                taken.welcome_pending = True
                taken.updated_at = now
            row.status = "used"
            await session.flush()
            return self._app_account(taken)

    async def account_for_platform(self, channel_type: str, peer_id: str) -> AccountRecord | None:
        """按平台用户标识找工作台绑定。找不到就不能猜账号。

        Args:
            channel_type: 企业渠道。
            peer_id: open_id 或 userId。

        Returns:
            绑定；没有时为 None。
        """
        if not peer_id:
            return None
        async with self.factory() as session:
            row = await session.scalar(
                select(ChannelAccount).where(
                    ChannelAccount.channel_type == channel_type,
                    ChannelAccount.ilink_user_id == peer_id[:128],
                    ChannelAccount.status != "unbound",
                )
            )
            return self._account(row) if row is not None else None

    async def record_app_event(
        self,
        app_id: UUID,
        message_id: str,
        peer_id: str,
        status: str,
        reason: str | None,
        excerpt: str,
    ) -> bool:
        """记录尚未绑定用户的入站。相同消息只记一次。

        Args:
            app_id: 企业应用。
            message_id: 平台消息标识。
            peer_id: 平台用户。
            status: received 或 skipped。
            reason: 跳过原因。
            excerpt: 短摘录。

        Returns:
            是否第一次看到。
        """
        if not message_id:
            return False
        stmt = (
            pg_insert(ChannelAppEvent)
            .values(
                id=uuid4(),
                app_id=app_id,
                peer_id=(peer_id or "")[:128],
                direction="inbound",
                message_id=_ledger_id(message_id, limit=128),
                status=status,
                skip_reason=reason,
                body_excerpt=excerpt[:500],
            )
            .on_conflict_do_nothing(constraint="uq_channel_app_events_message")
            .returning(ChannelAppEvent.id)
        )
        async with self.factory.begin() as session:
            inserted = await session.scalar(stmt)
        return inserted is not None

    async def next_passive_seq(self, account_id: UUID, workspace_id: UUID, peer_id: str, message_id: str) -> int | None:
        """为 QQ 被动回复分配 1 到 4 的序号。换了一条用户消息就重新计数。

        Args:
            account_id: 渠道账号。
            workspace_id: 当前空间。
            peer_id: QQ openid。
            message_id: 正在回复的用户消息。

        Returns:
            序号；这条消息的 4 次已经用完时为 None。
        """
        if not message_id:
            return None
        async with self.factory.begin() as session:
            peer = await self._lock_peer(session, account_id, workspace_id, peer_id)
            if peer.passive_message_id != message_id:
                peer.passive_message_id = message_id[:128]
                peer.passive_reply_count = 0
            if peer.passive_reply_count >= 4:
                return None
            peer.passive_reply_count += 1
            peer.updated_at = datetime.now(UTC)
            return peer.passive_reply_count

    async def workspace_role(self, user_id: UUID, workspace_id: UUID) -> str | None:
        """读取成员角色，供企业机器人配置做管理员校验。

        Args:
            user_id: 用户。
            workspace_id: 空间。

        Returns:
            admin 或 member；不是有效成员时为 None。
        """
        from app.models.identity import WorkspaceMembership

        async with self.factory() as session:
            role = await session.scalar(
                select(WorkspaceMembership.role).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.status == "active",
                )
            )
        return role if isinstance(role, str) else None

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

    def _app(self, row: ChannelApp) -> AppRecord:
        """解密企业应用密钥。失败时 secret 为空，发送会明确失败而不是用错凭证。

        Args:
            row: 应用行。

        Returns:
            内存中的应用记录。
        """
        secret = ""
        robot_code = ""
        sandbox = False
        public = row.public_config if isinstance(row.public_config, dict) else {}
        robot_value = public.get("robot_code")
        if isinstance(robot_value, str):
            robot_code = robot_value
        sandbox = public.get("sandbox") is True
        if row.credentials_ciphertext:
            try:
                import json

                loaded = json.loads(self.crypto.decrypt(row.credentials_ciphertext))
                if isinstance(loaded, dict):
                    secret = str(loaded.get("secret") or "")
                    if not robot_code and isinstance(loaded.get("robot_code"), str):
                        robot_code = loaded["robot_code"]
            except (ApplicationError, json.JSONDecodeError, TypeError):
                logger.warning("channel_app_secret_decrypt_failed", extra={"app_id": str(row.id)})
        return AppRecord(
            id=row.id,
            workspace_id=row.workspace_id,
            channel_type=row.channel_type,
            external_app_id=row.external_app_id,
            status=row.status,
            robot_code=robot_code,
            sandbox=sandbox,
            secret=secret,
        )

    def _app_account(self, row: ChannelAccount) -> AccountRecord:
        """企业绑定没有个人 token。"""
        return self._account(row, token="")

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
            app_id=row.app_id,
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
