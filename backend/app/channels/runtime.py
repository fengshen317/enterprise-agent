"""单进程微信长轮询。审批等待在本进程，所以必须和问答执行器在同一进程。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.channels.bus import ChannelBus
from app.channels.capabilities import capability_for, delivery_skip_reason
from app.channels.crypto import ChannelCrypto
from app.channels.handler import InboundHandler
from app.channels.ilink import ILinkClient
from app.channels.inbound import InboundEvent
from app.channels.records import AccountRecord, AppRecord, WorkspaceGate
from app.channels.repository import ChannelStore
from app.channels.wechat_text import progress_label
from app.core.config import Settings
from app.core.database import Database
from app.core.errors import ApplicationError
from app.models.identity import Workspace, WorkspaceMembership
from app.models.tool_call import ToolCall
from app.schemas.auth import ResourceScope
from app.schemas.chat import RunCreate, RunSnapshot
from app.services.agents import AgentService
from app.services.approvals import ApprovalNotice, ApprovalService
from app.services.chat_runtime import ChatRuntime

logger = logging.getLogger(__name__)
# 与问答锁、调度锁错开。第二段按账号散列，避免一个进程重复轮询同一账号。
CHANNEL_LOCK = 782341920
# 凭证解不开时不能紧循环打数据库。密钥轮换后每分钟再试一次。
TOKEN_RETRY_SECONDS = 60.0


def poll_wait_seconds(account: AccountRecord, now: datetime) -> float | None:
    """决定这一轮要不要先等待。None 表示可以立刻拉更新。

    Args:
        account: 当前账号。
        now: 当前时间。

    Returns:
        等待秒数；可以轮询时为 None。
    """
    if not account.token:
        return TOKEN_RETRY_SECONDS
    if account.paused_until is not None and account.paused_until > now:
        remaining = (account.paused_until - now).total_seconds()
        return min(5.0, max(0.2, remaining))
    return None


def advisory_key(account_id: UUID) -> int:
    """把账号 id 折成 PostgreSQL int4 锁键。

    Args:
        account_id: 渠道账号。

    Returns:
        0 到 2^31-1 的整数。
    """
    value = 0
    for byte in account_id.bytes:
        value = (value * 131 + byte) & 0x7FFFFFFF
    return value


class ChatGateway:
    """把渠道调用接到现有 ChatRuntime，不另开生成链路。"""

    def __init__(self, runtime: ChatRuntime) -> None:
        """保存问答运行时。

        Args:
            runtime: 应用的问答执行器。
        """
        self.runtime = runtime

    async def ensure_ready(self) -> None:
        """获取问答进程锁。"""
        await self.runtime.ensure_ready()

    async def create_conversation(self, scope: ResourceScope, title: str) -> UUID:
        """创建属于该用户和空间的对话。

        Args:
            scope: 用户和空间。
            title: 对话标题。

        Returns:
            对话标识。
        """
        view = await self.runtime.store.create_conversation(title, scope)
        return view.id

    async def create_run(
        self, scope: ResourceScope, conversation_id: UUID, content: str, idempotency_key: str
    ) -> RunSnapshot:
        """按定时任务同一方式创建运行。

        Args:
            scope: 用户和空间。
            conversation_id: 目标对话。
            content: 用户问题。
            idempotency_key: 微信消息幂等键。

        Returns:
            运行快照。
        """
        return await self.runtime.create_run(
            conversation_id, RunCreate(content=content, idempotency_key=idempotency_key), scope
        )

    def schedule(self, run_id: UUID, request_id: str) -> None:
        """在持有问答锁的进程里执行。

        Args:
            run_id: 运行标识。
            request_id: 日志关联标识。
        """
        self.runtime.schedule(run_id, request_id)

    async def snapshot(self, run_id: UUID) -> RunSnapshot:
        """读取运行快照。

        Args:
            run_id: 运行标识。

        Returns:
            最新快照。
        """
        return await self.runtime.store.snapshot(run_id)

    async def activity(self, run_id: UUID) -> str:
        """用最近一条未结束的工具调用说明进度。

        Args:
            run_id: 问答运行。

        Returns:
            一句进度。
        """
        async with self.runtime.database.session_factory() as session:
            row = (
                await session.execute(
                    select(ToolCall.tool_name, ToolCall.status)
                    .where(ToolCall.run_id == run_id, ToolCall.status.in_(("running", "awaiting_approval")))
                    .order_by(ToolCall.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return "仍在处理…"
        if row.status == "awaiting_approval":
            return "正在等待确认…"
        return progress_label(row.tool_name)


class AgentGateway:
    """复用 Agent 服务的可见性和切换检查。"""

    def __init__(self, agents: AgentService) -> None:
        """保存 Agent 服务。

        Args:
            agents: 应用的 Agent 服务。
        """
        self.agents = agents

    async def list_agents(self, scope: ResourceScope) -> list[tuple[UUID, str]]:
        """列出当前用户能使用的 Agent。

        Args:
            scope: 用户和空间。

        Returns:
            标识和名称。
        """
        views = await self.agents.list_agents(scope, 0, 50)
        return [(item.id, item.name) for item in views]

    async def switch(self, scope: ResourceScope, conversation_id: UUID, agent_id: UUID | None) -> None:
        """切换对话 Agent。

        Args:
            scope: 用户和空间。
            conversation_id: 对话。
            agent_id: 目标 Agent，None 为内置助手。
        """
        await self.agents.switch_conversation_agent(scope, conversation_id, agent_id)


class MembershipGateway:
    """按 slug 确认用户是目标空间的有效成员。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """保存会话工厂。

        Args:
            factory: 数据库会话工厂。
        """
        self.factory = factory

    async def resolve(self, user_id: UUID, slug: str) -> WorkspaceGate:
        """查找空间并检查成员资格。

        Args:
            user_id: 系统用户。
            slug: 空间标识。

        Returns:
            是否存在、是否允许进入。
        """
        async with self.factory() as session:
            workspace = await session.scalar(select(Workspace).where(Workspace.slug == slug))
            if workspace is None or workspace.status != "active":
                return WorkspaceGate(found=False, allowed=False)
            membership = await session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace.id,
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.status == "active",
                )
            )
            if membership is None:
                return WorkspaceGate(found=True, allowed=False, workspace_id=workspace.id, name=workspace.name)
            return WorkspaceGate(found=True, allowed=True, workspace_id=workspace.id, name=workspace.name)


class ApprovalGateway:
    """把微信里的同意或拒绝交给审批服务，沿用所有者校验。"""

    def __init__(self, approvals: ApprovalService) -> None:
        """保存审批服务。

        Args:
            approvals: 应用的审批服务。
        """
        self.approvals = approvals

    async def decide(
        self, scope: ResourceScope, run_id: UUID, approval_id: UUID, decision: str, comment: str | None
    ) -> str:
        """执行审批决定。

        Args:
            scope: 绑定用户和审批所在空间。
            run_id: 运行。
            approval_id: 审批。
            decision: approve 或 reject。
            comment: 可选说明。

        Returns:
            审批状态。
        """
        view = await self.approvals.decide(scope, run_id, approval_id, decision, comment)
        return view.status


class ChannelRuntime:
    """每个有效账号一条长轮询。只有拿到问答进程锁的进程才会轮询。"""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        crypto: ChannelCrypto,
        ilink: ILinkClient,
        repo: ChannelStore,
        chat_runtime: ChatRuntime,
        approval_service: ApprovalService,
        agent_service: AgentService,
        bus: ChannelBus | None = None,
    ) -> None:
        """装配轮询器和入站处理器。

        Args:
            database: 用来持有 advisory lock 的连接池。
            settings: 渠道配置。
            crypto: 判断渠道是否启用。
            ilink: iLink 客户端。
            repo: 渠道存储。
            chat_runtime: 问答执行器。
            approval_service: 审批服务。
            agent_service: Agent 服务。
            bus: 企业渠道出站总线。微信测试可以不传。
        """
        self.database = database
        self.settings = settings
        self.crypto = crypto
        self.ilink = ilink
        self.repo = repo
        self.chat_runtime = chat_runtime
        self.bus = bus
        self.handler = InboundHandler(
            settings,
            repo,
            bus or ilink,
            ChatGateway(chat_runtime),
            AgentGateway(agent_service),
            MembershipGateway(database.session_factory),
            ApprovalGateway(approval_service),
            log_recorder=chat_runtime.log_recorder,
        )
        self.closing = False
        self._supervisor: asyncio.Task[None] | None = None
        self._pollers: dict[UUID, asyncio.Task[None]] = {}
        self._app_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._timeouts: dict[UUID, float] = {}

    async def start(self) -> None:
        """在密钥可用时启动监督循环。"""
        if not self.settings.channel_enabled or not self.crypto.configured or self._supervisor is not None:
            return
        self._supervisor = asyncio.create_task(self._supervise())

    async def close(self) -> None:
        """停止轮询和尚未完成的微信回复。"""
        self.closing = True
        tasks = [
            task for task in (self._supervisor, *self._pollers.values(), *self._app_tasks.values()) if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._supervisor = None
        self._pollers.clear()
        self._app_tasks.clear()
        await self.handler.cancel()
        if self.bus is not None:
            await self.bus.aclose()

    async def poll_once(self, account: AccountRecord) -> None:
        """拉一次更新。-14 时把账号暂停配置的时长，不处理这批消息。

        Args:
            account: 当前账号快照。暂停状态会写回数据库。
        """
        now = datetime.now(UTC)
        if poll_wait_seconds(account, now) is not None:
            return
        if account.status == "paused":
            await self.repo.mark_active(account.id)
            account.status = "active"
            account.paused_until = None
        page = await self.ilink.get_updates(account, self._timeouts.get(account.id))
        if page.long_poll_ms is not None and page.long_poll_ms > 0:
            self._timeouts[account.id] = page.long_poll_ms / 1000
        if page.session_expired:
            until = now + timedelta(seconds=self.settings.ilink_session_pause_seconds)
            await self.repo.mark_paused(account.id, until)
            account.status = "paused"
            account.paused_until = until
            return
        # 先处理完本批再推进游标。中途失败时下次长轮询还会带回这些消息，已入库的由 message_id 去重。
        for message in page.messages:
            await self.handler.handle(account, message)
        if page.cursor and page.cursor != account.updates_cursor:
            await self.repo.save_cursor(account.id, page.cursor)
            account.updates_cursor = page.cursor
        try:
            await self.handler.remind_if_due(account)
        except Exception:
            logger.warning("wechat_reminder_failed", extra={"account_id": str(account.id)})

    async def on_run_finished(self, run_id: UUID) -> None:
        """仅把明确开启微信推送的定时任务结果推回微信。

        Args:
            run_id: 已结束的运行。
        """
        if run_id in self.handler.direct_runs or not self._enabled():
            return
        delivery = await self.repo.run_delivery(run_id)
        if delivery is None or delivery.status not in {"completed", "failed"}:
            return
        scheduled = delivery.idempotency_key.startswith(("schedule_", "manual_"))
        channels = list(delivery.push_channels)
        if not channels and delivery.push_to_wechat:
            channels = ["ilink"]
        # 任务没有勾选任何渠道时直接返回，这是用户选择不投递，不是发送失败。
        if not scheduled or delivery.schedule_id is None or not channels:
            return
        if delivery.status == "failed":
            body = delivery.error_message or "生成失败，请在网页查看。"
        elif delivery.content.strip():
            body = delivery.content.strip()
        else:
            body = "这次回答没有生成正文，请在网页查看。"
        label = f"【定时问答 · {delivery.schedule_name}】"
        for channel in channels:
            account = await self.repo.account_for_user(delivery.user_id, channel)
            if account is None:
                continue
            await self.handler.push_proactive(
                account,
                f"{label}\n{body}",
                run_id=run_id,
                workspace_id=delivery.workspace_id,
                conversation_id=delivery.conversation_id,
            )

    async def on_approval(self, notice: ApprovalNotice) -> None:
        """把写操作审批发到绑定微信。窗口外只记录原因。

        Args:
            notice: 审批摘要。
        """
        if not self._enabled():
            return
        for account in await self._bound_accounts(notice.user_id):
            await self._push_approval(account, notice)

    async def _bound_accounts(self, user_id: UUID) -> list[AccountRecord]:
        """列出这个用户还没解绑的渠道。旧测试替身只有按微信查找的方法。"""
        listed = getattr(self.repo, "accounts_for_user", None)
        if listed is not None:
            rows: list[AccountRecord] = await listed(user_id)
            return rows
        account = await self.repo.account_for_user(user_id)
        return [account] if account is not None else []

    async def _push_approval(self, account: AccountRecord, notice: ApprovalNotice) -> None:
        """按渠道能力决定审批能不能推送。删除和外发仍然只给网页链接。"""
        peer = await self.repo.latest_peer(account.id)
        caps = capability_for(account.channel_type)
        reason = delivery_skip_reason(caps, peer, datetime.now(UTC), self.settings)
        if reason is not None or (peer is None and caps.proactive_uses_reply_window):
            await self.repo.log_outbound(
                account.id,
                peer.peer_id if peer is not None else "",
                "skipped",
                reason or "no_inbound",
                None,
                notice.tool_name[:500],
            )
            return
        try:
            code = await self.repo.issue_approval_code(account.id, notice)
        except ApplicationError:
            logger.warning("channel_approval_code_failed", extra={"approval_id": str(notice.approval_id)})
            return
        label = caps.label
        if notice.risk_level in {"delete", "external"}:
            page = self.settings.public_app_base_url.rstrip("/") + "/chat"
            text = (
                f"需要在网页确认：{notice.tool_name}\n内容：{notice.preview}\n删除和外发不在{label}里决定。打开 {page}"
            )
            web_only = True
            reply_code: str | None = None
        else:
            text = (
                "需要你确认一次写操作。\n"
                f"工具：{notice.tool_name}\n"
                f"内容：{notice.preview}\n"
                f"回复「同意 {code}」或「拒绝 {code} 原因」。只有这一项时也可以只回复「同意」或「拒绝」。"
            )
            web_only = False
            reply_code = code
        target = peer.peer_id if peer is not None else account.ilink_user_id
        token = peer.context_token if peer is not None else ""
        sender = getattr(self.handler.ilink, "send_approval", None)
        if sender is not None:
            try:
                await sender(
                    account,
                    target,
                    token,
                    text,
                    code=reply_code,
                    web_only=web_only,
                )
                await self.repo.log_outbound(account.id, target, "sent", None, None, text[:500])
                return
            except Exception:
                logger.info("channel_approval_card_failed", extra={"account_id": str(account.id)})
        await self.handler.send_text(account, target, token, text, run_id=None, workspace_id=notice.workspace_id)

    async def _on_enterprise(self, app: AppRecord, event: InboundEvent) -> None:
        """长连接收到的消息：已绑定的进入问答，未绑定的只回绑定说明。"""
        account = None
        if event.peer_id:
            account = await self.repo.account_for_platform(app.channel_type, event.peer_id)
        if account is None:
            await self.handler.handle_unbound(app, event)
            return
        await self.handler.handle_event(account, event)

    def _enabled(self) -> bool:
        """渠道是否配置完成。

        Returns:
            是否处理推送。
        """
        return self.settings.channel_enabled and self.crypto.configured

    async def _supervise(self) -> None:
        """只在本进程拥有问答锁时为每个账号启动一条轮询。"""
        while not self.closing:
            try:
                await self.chat_runtime.ensure_ready()
                accounts = await self.repo.list_polling_accounts()
                for account in accounts:
                    current = self._pollers.get(account.id)
                    if current is None or current.done():
                        self._pollers[account.id] = asyncio.create_task(self._poll_account(account.id))
                if self.bus is not None:
                    apps = await self.repo.list_active_apps()
                    live = {app.id for app in apps}
                    for app in apps:
                        running = self._app_tasks.get(app.id)
                        if running is None or running.done():
                            self._app_tasks[app.id] = asyncio.create_task(self._consume_app(app.id))
                    for app_id, task in list(self._app_tasks.items()):
                        if app_id not in live and not task.done():
                            task.cancel()
                await self._sleep(2)
            except asyncio.CancelledError:
                return
            except ApplicationError as exc:
                logger.warning("wechat_waiting_for_chat_lock", extra={"error_code": exc.code})
                await self._sleep(2)
            except Exception:
                logger.warning("wechat_supervisor_failed")
                await self._sleep(2)

    async def _consume_app(self, app_id: UUID) -> None:
        """一个企业机器人只保留一条长连接。集群模式下也只有拿到锁的进程会收到事件。

        Args:
            app_id: 企业应用。
        """
        bus = self.bus
        if bus is None:
            return
        connection = await self.database.engine.connect()
        held = False
        try:
            held = await self._try_lock(connection, app_id)
            if not held:
                return
            app = await self.repo.get_app(app_id)
            if app is None or app.status != "active":
                return

            async def dispatch(event: InboundEvent) -> None:
                await self._on_enterprise(app, event)

            if app.channel_type == "feishu":
                await bus.feishu.serve(app, dispatch)
            elif app.channel_type == "dingtalk":
                await bus.dingtalk.serve(app, dispatch)
            elif app.channel_type == "qq":
                await bus.qq.serve(app, dispatch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("channel_app_consume_failed", extra={"app_id": str(app_id)})
        finally:
            if held:
                await self._unlock(connection, app_id)
            await connection.close()
            self._app_tasks.pop(app_id, None)

    async def _poll_account(self, account_id: UUID) -> None:
        """持有该账号的 advisory lock 后持续长轮询。

        Args:
            account_id: 渠道账号。
        """
        connection = await self.database.engine.connect()
        held = False
        try:
            held = await self._try_lock(connection, account_id)
            if not held:
                return
            await self._notify(account_id, start=True)
            while not self.closing:
                account = await self.repo.get_account(account_id)
                if account is None or account.status == "unbound":
                    break
                wait = poll_wait_seconds(account, datetime.now(UTC))
                if wait is not None:
                    if not account.token:
                        logger.warning("ilink_token_unavailable", extra={"account_id": str(account_id)})
                    await self._sleep(wait)
                    continue
                try:
                    await self.poll_once(account)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("ilink_poll_failed", extra={"account_id": str(account_id)})
                    await self._sleep(2)
        finally:
            if held:
                await self._notify(account_id, start=False)
                await self._unlock(connection, account_id)
            await connection.close()
            self._pollers.pop(account_id, None)

    async def _try_lock(self, connection: AsyncConnection, account_id: UUID) -> bool:
        """尝试获取账号锁。

        Args:
            connection: 专用于持锁的连接。
            account_id: 渠道账号。

        Returns:
            是否获得锁。
        """
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(CAST(:k1 AS integer), CAST(:k2 AS integer))"),
            {"k1": CHANNEL_LOCK, "k2": advisory_key(account_id)},
        )
        await connection.commit()
        return bool(acquired)

    async def _unlock(self, connection: AsyncConnection, account_id: UUID) -> None:
        """释放账号锁。

        Args:
            connection: 持锁连接。
            account_id: 渠道账号。
        """
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(CAST(:k1 AS integer), CAST(:k2 AS integer))"),
                {"k1": CHANNEL_LOCK, "k2": advisory_key(account_id)},
            )
            await connection.commit()
        except Exception:
            logger.warning("wechat_unlock_failed", extra={"account_id": str(account_id)})

    async def _notify(self, account_id: UUID, *, start: bool) -> None:
        """最好努力地通知 iLink 客户端启动或停止。

        Args:
            account_id: 渠道账号。
            start: True 为 notifystart。
        """
        account = await self.repo.get_account(account_id)
        if account is None or not account.token:
            return
        try:
            if start:
                await self.ilink.notify_start(account)
            else:
                await self.ilink.notify_stop(account)
        except Exception:
            logger.warning("ilink_notify_failed", extra={"account_id": str(account_id), "start": start})

    async def _sleep(self, seconds: float) -> None:
        """可被关闭打断的等待。

        Args:
            seconds: 最长等待秒数。
        """
        try:
            await asyncio.sleep(max(0.05, seconds))
        except asyncio.CancelledError:
            raise
