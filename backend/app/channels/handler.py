"""把微信入站消息变成对话运行、指令或审批决定。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID, uuid4

from app.channels.commands import chunk_text, parse_inbound
from app.channels.ilink import ILinkClient, ILinkMessage
from app.channels.records import AccountRecord, ApprovalTicket, PeerLink, WorkspaceGate
from app.channels.repository import ChannelStore
from app.channels.wechat_text import wechat_reply
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope
from app.schemas.chat import RunSnapshot

logger = logging.getLogger(__name__)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_WEB_ONLY_RISKS = frozenset({"delete", "external"})
WELCOME_TEXT = (
    "已绑定微信，直接发消息就能对话。\n"
    "指令：/new 新对话，/ws 空间标识 切换空间，/agent 切换 Agent，/help 再看一遍说明。\n"
    "只有一项待确认的写操作时，可以回复「同意」或「1」，也可以回复「拒绝」或「2」加原因。"
    "有多项时请带上确认码。删除和外发只能在网页里确认。\n"
    "超过 24 小时没有发消息，定时结果不会推到微信。窗口快结束时我会提醒你回复任意内容。"
)
REMINDER_TEXT = "回复任意内容可继续接收推送"
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class ChatPort(Protocol):
    """渠道调用问答运行时的最小表面。"""

    async def ensure_ready(self) -> None:
        """获得本进程的问答执行权。"""

    async def create_conversation(self, scope: ResourceScope, title: str) -> UUID:
        """创建对话。"""

    async def create_run(
        self, scope: ResourceScope, conversation_id: UUID, content: str, idempotency_key: str
    ) -> RunSnapshot:
        """创建运行。"""

    def schedule(self, run_id: UUID, request_id: str) -> None:
        """在本进程执行运行。"""

    async def snapshot(self, run_id: UUID) -> RunSnapshot:
        """读取运行快照。"""

    async def activity(self, run_id: UUID) -> str:
        """当前运行正在做什么，供进度提示使用。"""


class AgentPort(Protocol):
    """列出和切换当前用户可用的 Agent。"""

    async def list_agents(self, scope: ResourceScope) -> list[tuple[UUID, str]]:
        """返回 Agent 标识和名称。"""

    async def switch(self, scope: ResourceScope, conversation_id: UUID, agent_id: UUID | None) -> None:
        """切换对话 Agent。None 表示内置序列助手。"""


class MembershipPort(Protocol):
    """按空间 slug 检查成员资格。"""

    async def resolve(self, user_id: UUID, slug: str) -> WorkspaceGate:
        """解析空间并检查成员。"""


class ApprovalPort(Protocol):
    """把微信回复交给现有审批服务。"""

    async def decide(
        self, scope: ResourceScope, run_id: UUID, approval_id: UUID, decision: str, comment: str | None
    ) -> str:
        """批准或拒绝，返回审批状态。"""


def window_skip_reason(peer: PeerLink | None, now: datetime, hours: int) -> str | None:
    """判断主动推送是否还在用户上次发消息后的窗口内。

    Args:
        peer: 最近入站的对端。
        now: 当前时间。
        hours: 窗口小时数。

    Returns:
        跳过原因；可以发送时为 None。
    """
    if peer is None or peer.last_inbound_at is None:
        return "no_inbound"
    inbound = peer.last_inbound_at
    if inbound.tzinfo is None:
        inbound = inbound.replace(tzinfo=UTC)
    if now - inbound > timedelta(hours=hours):
        return "outside_window"
    if not peer.context_token:
        return "no_context_token"
    return None


def wechat_idempotency_key(message_id: str) -> str:
    """用消息 id 生成问答幂等键，避免微信重试创建两次运行。

    Args:
        message_id: iLink 消息 id。

    Returns:
        符合 RunCreate 规则的键。
    """
    raw = f"wx{message_id}"
    if _IDEMPOTENCY.fullmatch(raw):
        return raw
    return "wx" + hashlib.sha256(message_id.encode()).hexdigest()[:40]


class InboundHandler:
    """处理一个账号收到的文本。长轮询本身不等待模型生成。"""

    def __init__(
        self,
        settings: Settings,
        repo: ChannelStore,
        ilink: ILinkClient,
        chat: ChatPort,
        agents: AgentPort,
        membership: MembershipPort,
        approvals: ApprovalPort,
    ) -> None:
        """绑定依赖。

        Args:
            settings: 窗口和分段长度。
            repo: 渠道存储。
            ilink: iLink 客户端。
            chat: 问答运行时。
            agents: Agent 列表和切换。
            membership: 空间成员检查。
            approvals: 审批决定。
        """
        self.settings = settings
        self.repo = repo
        self.ilink = ilink
        self.chat = chat
        self.agents = agents
        self.membership = membership
        self.approvals = approvals
        self.direct_runs: set[UUID] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Coroutine[object, object, None]) -> None:
        """持有后台任务引用，避免回复协程被回收。

        Args:
            coro: 回复或等待协程。
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """等待已发出的回复任务，供测试使用。"""
        if self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def cancel(self) -> None:
        """停机时取消尚未发出的回复。"""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def handle(self, account: AccountRecord, message: ILinkMessage) -> None:
        """处理一条入站消息。群消息、他人消息和重复消息直接跳过。

        Args:
            account: 当前绑定。处理过程中可能改掉它的当前空间。
            message: 规范化后的 iLink 消息。
        """
        if message.group_id.strip():
            await self.repo.record_inbound(
                account.id, message.message_id, message.from_user_id, "skipped", "group", message.text[:500]
            )
            return
        if message.message_type not in (None, 1):
            await self.repo.record_inbound(
                account.id, message.message_id, message.from_user_id, "skipped", "not_user", message.text[:500]
            )
            return
        if not message.from_user_id or message.from_user_id != account.ilink_user_id:
            await self.repo.record_inbound(
                account.id, message.message_id, message.from_user_id, "skipped", "unexpected_peer", message.text[:500]
            )
            return
        # 生成中的增量不占去重位，避免后面的完成态被丢掉。服务端是否会用同一 id 再推一次，尚未在真机验证。
        if message.message_state == 1:
            return
        # 没有 message_id 时无法去重。若共用同一个幂等键，第二条及以后的文本会被静默丢掉。
        if not message.message_id:
            message = ILinkMessage(
                f"local-{uuid4().hex}",
                message.from_user_id,
                message.to_user_id,
                message.group_id,
                message.context_token,
                message.text,
                message.message_type,
                message.message_state,
            )
        inserted = await self.repo.record_inbound(
            account.id, message.message_id, message.from_user_id, "received", None, message.text[:500]
        )
        if not inserted:
            return
        await self.repo.touch_inbound(
            account.id, account.workspace_id, message.from_user_id, message.context_token, datetime.now(UTC)
        )
        text = message.text.strip()
        if not text:
            await self.reply(account, message, "目前只能处理文字消息。")
            return
        command = parse_inbound(text)
        if account.welcome_pending:
            await self.reply(account, message, WELCOME_TEXT)
            await self.repo.clear_welcome(account.id)
            account.welcome_pending = False
            if command.kind == "help":
                return
        if command.kind == "help":
            await self.reply(account, message, WELCOME_TEXT)
        elif command.kind == "approve":
            await self._decide(account, message, command.code, "approve", None)
        elif command.kind == "reject":
            await self._decide(account, message, command.code, "reject", command.comment or None)
        elif command.kind == "new":
            await self._new(account, message)
        elif command.kind == "workspace":
            await self._workspace(account, message, command.argument)
        elif command.kind == "agent":
            await self._agent(account, message, command.argument)
        else:
            await self._chat(account, message, text)

    async def push_proactive(self, account: AccountRecord, text: str, *, run_id: UUID | None) -> None:
        """在 24 小时窗口内主动推送。窗口外只记原因，网页里的回答保留。

        Args:
            account: 接收账号。
            text: 已带上来源前缀的正文。
            run_id: 关联运行。为空时不去重。
        """
        if run_id is not None and await self.repo.outbound_exists(run_id):
            return
        peer = await self.repo.latest_peer(account.id)
        reason = window_skip_reason(peer, datetime.now(UTC), self.settings.channel_reply_window_hours)
        if reason is not None or peer is None:
            await self.repo.log_outbound(
                account.id,
                peer.peer_id if peer is not None else "",
                "skipped",
                reason or "no_inbound",
                run_id,
                text[:500],
            )
            return
        await self.send_text(account, peer.peer_id, peer.context_token, text, run_id=run_id)

    async def reply(self, account: AccountRecord, message: ILinkMessage, text: str) -> None:
        """回复刚刚收到的消息，优先用这条消息自己的 context_token。

        Args:
            account: 发送账号。
            message: 入站消息。
            text: 回复正文。
        """
        link = await self.repo.get_peer(account.id, account.workspace_id, message.from_user_id)
        token = message.context_token or (link.context_token if link is not None else "")
        await self.send_text(account, message.from_user_id, token, text, run_id=None)

    async def send_text(
        self, account: AccountRecord, to_user_id: str, context_token: str, text: str, *, run_id: UUID | None
    ) -> None:
        """分段发送并写出站日志。

        Args:
            account: 发送账号。
            to_user_id: 接收用户。
            context_token: 会话令牌。
            text: 完整正文。
            run_id: 关联运行。
        """
        chunks = chunk_text(text, self.settings.ilink_message_chunk_characters)
        if not chunks:
            return
        try:
            for chunk in chunks:
                await self.ilink.send_text(account, to_user_id, context_token, chunk)
        except Exception:
            logger.warning("ilink_send_failed", extra={"account_id": str(account.id), "run_id": str(run_id or "")})
            await self.repo.log_outbound(account.id, to_user_id, "failed", "send_failed", run_id, text[:500])
            return
        await self.repo.log_outbound(account.id, to_user_id, "sent", None, run_id, text[:500])

    async def _new(self, account: AccountRecord, message: ILinkMessage) -> None:
        """开启一条新的网页对话并让后续消息进入它。

        Args:
            account: 当前账号。
            message: /new 消息。
        """
        scope = ResourceScope(account.user_id, account.workspace_id)
        try:
            conversation_id = await self.chat.create_conversation(scope, "微信对话")
        except ApplicationError as exc:
            await self.reply(account, message, exc.message)
            return
        await self.repo.set_conversation(account.id, account.workspace_id, message.from_user_id, conversation_id)
        await self.reply(account, message, "已开始新对话。")

    async def _workspace(self, account: AccountRecord, message: ILinkMessage, slug: str) -> None:
        """切换当前空间。不是成员时不修改绑定。

        Args:
            account: 当前账号，成功时会改写 workspace_id。
            message: /ws 消息。
            slug: 空间标识。
        """
        if not _SLUG.fullmatch(slug):
            await self.reply(account, message, "空间标识不正确。用法：/ws 空间标识")
            return
        gate = await self.membership.resolve(account.user_id, slug)
        if not gate.found:
            await self.reply(account, message, "没有找到这个空间。")
            return
        if not gate.allowed or gate.workspace_id is None:
            await self.reply(account, message, "你不是该空间的成员，未切换。")
            return
        await self.repo.set_workspace(account.id, gate.workspace_id)
        account.workspace_id = gate.workspace_id
        await self._ensure_conversation(account, message.from_user_id)
        await self.reply(account, message, f"已切换到空间 {gate.name}。")

    async def _agent(self, account: AccountRecord, message: ILinkMessage, argument: str) -> None:
        """列出或切换当前对话的 Agent。

        Args:
            account: 当前账号。
            message: /agent 消息。
            argument: 空字符串表示列表，否则是名称。
        """
        scope = ResourceScope(account.user_id, account.workspace_id)
        try:
            conversation_id = await self._ensure_conversation(account, message.from_user_id)
            agents = await self.agents.list_agents(scope)
        except ApplicationError as exc:
            await self.reply(account, message, exc.message)
            return
        if not argument:
            lines = ["当前可用 Agent：", "- 序列助手（默认）", *(f"- {name}" for _, name in agents)]
            lines.append("发送 /agent 名称 切换，或 /agent 默认 回到序列助手。")
            await self.reply(account, message, "\n".join(lines))
            return
        if argument in {"默认", "序列助手", "default"}:
            await self._switch_agent(account, message, scope, conversation_id, None, "序列助手")
            return
        chosen = _match_agent(agents, argument)
        if isinstance(chosen, str):
            await self.reply(account, message, chosen)
            return
        await self._switch_agent(account, message, scope, conversation_id, chosen[0], chosen[1])

    async def _switch_agent(
        self,
        account: AccountRecord,
        message: ILinkMessage,
        scope: ResourceScope,
        conversation_id: UUID,
        agent_id: UUID | None,
        name: str,
    ) -> None:
        """执行切换并回复结果。

        Args:
            account: 当前账号。
            message: 入站消息。
            scope: 用户和空间。
            conversation_id: 当前对话。
            agent_id: 目标 Agent。
            name: 回复里展示的名称。
        """
        try:
            await self.agents.switch(scope, conversation_id, agent_id)
        except ApplicationError as exc:
            await self.reply(account, message, exc.message)
            return
        await self.reply(account, message, f"已切换为 {name}。")

    async def _decide(
        self, account: AccountRecord, message: ILinkMessage, code: str, decision: str, comment: str | None
    ) -> None:
        """用短码调用审批服务。权限检查仍在审批服务里。

        Args:
            account: 当前账号。
            message: 同意或拒绝消息。
            code: 四位短码。
            decision: approve 或 reject。
            comment: 拒绝原因。
        """
        ticket: ApprovalTicket
        if not code:
            chosen = await self._bare_ticket(account, message)
            if chosen == "chat":
                await self._chat(account, message, message.text.strip())
                return
            if not isinstance(chosen, ApprovalTicket):
                return
            ticket = chosen
        else:
            found = await self.repo.find_approval_code(account.id, code)
            if found is None:
                await self.reply(account, message, "没有找到这个确认码，或它已过期。")
                return
            if found.risk_level in _WEB_ONLY_RISKS:
                await self.reply(account, message, self._web_only_text(found))
                return
            ticket = found
        scope = ResourceScope(account.user_id, ticket.workspace_id)
        try:
            status = await self.approvals.decide(scope, ticket.run_id, ticket.approval_id, decision, comment)
        except ApplicationError as exc:
            await self.reply(account, message, exc.message)
            return
        await self.repo.consume_approval_code(account.id, ticket.code)
        if status == "approved":
            await self.reply(account, message, "已同意。")
        elif status == "rejected":
            await self.reply(account, message, "已拒绝。")
        else:
            await self.reply(account, message, f"审批状态：{status}")

    async def _chat(self, account: AccountRecord, message: ILinkMessage, text: str) -> None:
        """创建问答运行，并在后台等待完成后回发。

        Args:
            account: 当前账号。
            message: 用户问题。
            text: 问题正文。
        """
        scope = ResourceScope(account.user_id, account.workspace_id)
        try:
            conversation_id = await self._ensure_conversation(account, message.from_user_id)
            await self.chat.ensure_ready()
            snapshot = await self.chat.create_run(
                scope, conversation_id, text[:20000], wechat_idempotency_key(message.message_id)
            )
        except ApplicationError as exc:
            if exc.code == "RUN_IN_PROGRESS":
                await self.reply(account, message, "上一条还在处理，请稍后再发。")
            else:
                await self.reply(account, message, exc.message)
            return
        if snapshot.run.status != "pending":
            return
        run_id = snapshot.run.id
        self.direct_runs.add(run_id)
        self.chat.schedule(run_id, f"wechat-{run_id.hex[:8]}")
        token = message.context_token
        self.spawn(self._deliver(account, message.from_user_id, token, run_id, snapshot.run.conversation_id))

    async def _deliver(
        self, account: AccountRecord, to_user_id: str, context_token: str, run_id: UUID, conversation_id: UUID
    ) -> None:
        """等运行结束，把最终正文发回微信。超过阈值时穿插进度。

        Args:
            account: 发送账号。
            to_user_id: 接收用户。
            context_token: 触发这条回复的令牌。
            run_id: 问答运行。
            conversation_id: 网页上打开完整回答的对话。
        """
        progress = asyncio.create_task(self._progress(account, to_user_id, context_token, run_id))
        try:
            await self.ilink.send_typing(account, to_user_id, context_token, True)
            deadline = monotonic() + self.settings.chat_run_timeout_seconds + 15
            snapshot: RunSnapshot | None = None
            while monotonic() < deadline:
                snapshot = await self.chat.snapshot(run_id)
                if snapshot.run.status not in {"pending", "running"}:
                    break
                await asyncio.sleep(0.4)
            body = wechat_reply(
                _final_text(snapshot),
                preview_characters=self.settings.channel_reply_preview_characters,
                page_url=self._page_url(conversation_id),
            )
            await self.send_text(account, to_user_id, context_token, body, run_id=run_id)
        finally:
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)
            self.direct_runs.discard(run_id)
            await self.ilink.send_typing(account, to_user_id, context_token, False)

    async def _progress(self, account: AccountRecord, to_user_id: str, context_token: str, run_id: UUID) -> None:
        """运行偏久时发一句进度，相同内容不重复刷屏。

        Args:
            account: 发送账号。
            to_user_id: 接收用户。
            context_token: 当前会话令牌。
            run_id: 问答运行。
        """
        await asyncio.sleep(self.settings.channel_progress_after_seconds)
        last = ""
        while True:
            label = await self.chat.activity(run_id)
            if label and label != last and context_token:
                await self.send_text(account, to_user_id, context_token, label, run_id=None)
                await self.ilink.send_typing(account, to_user_id, context_token, True)
                last = label
            await asyncio.sleep(self.settings.channel_progress_interval_seconds)

    async def remind_if_due(self, account: AccountRecord) -> None:
        """窗口还剩一小段时间时提醒一次。已经关闭的窗口不再发。

        Args:
            account: 当前账号。
        """
        hours = self.settings.channel_window_reminder_hours
        if hours <= 0:
            return
        peer = await self.repo.latest_peer(account.id)
        if window_skip_reason(peer, datetime.now(UTC), self.settings.channel_reply_window_hours) is not None:
            return
        if peer is None or peer.last_inbound_at is None:
            return
        inbound = peer.last_inbound_at if peer.last_inbound_at.tzinfo else peer.last_inbound_at.replace(tzinfo=UTC)
        remaining = inbound + timedelta(hours=self.settings.channel_reply_window_hours) - datetime.now(UTC)
        if remaining <= timedelta(0) or remaining > timedelta(hours=hours):
            return
        if peer.reminder_for_inbound_at == inbound:
            return
        await self.send_text(account, peer.peer_id, peer.context_token, REMINDER_TEXT, run_id=None)
        await self.repo.mark_reminder(account.id, peer.workspace_id, peer.peer_id, inbound)

    async def _bare_ticket(
        self, account: AccountRecord, message: ILinkMessage
    ) -> ApprovalTicket | Literal["chat", "done"]:
        """没有确认码时，只有恰好一项可在微信处理的审批才直接决定。

        Args:
            account: 当前账号。
            message: 同意或拒绝消息。

        Returns:
            可决定的票据、继续当普通对话，或已经回复过。
        """
        pending = await self.repo.list_pending_codes(account.id)
        if not pending:
            return "chat"
        if len(pending) == 1 and pending[0].risk_level in _WEB_ONLY_RISKS:
            await self.reply(account, message, self._web_only_text(pending[0]))
            return "done"
        writable = [item for item in pending if item.risk_level not in _WEB_ONLY_RISKS]
        if len(pending) == 1 and len(writable) == 1:
            return writable[0]
        lines = ["有多项待确认，请带上确认码："]
        for item in pending:
            if item.risk_level in _WEB_ONLY_RISKS:
                lines.append(f"- {item.tool_name or '操作'} 只能在网页确认")
            else:
                lines.append(f"- {item.tool_name or '操作'}：同意 {item.code} / 拒绝 {item.code}")
        await self.reply(account, message, "\n".join(lines))
        return "done"

    def _web_only_text(self, ticket: ApprovalTicket) -> str:
        """删除和外发不在微信里决定。

        Args:
            ticket: 高风险审批。

        Returns:
            指向网页的说明。
        """
        name = ticket.tool_name or "这项操作"
        return f"{name}属于删除或外发，只能在网页确认：{self._page_url(None)}"

    def _page_url(self, conversation_id: UUID | None) -> str:
        """拼网页地址。没有对话时回到问答首页。

        Args:
            conversation_id: 对话标识。

        Returns:
            绝对地址。
        """
        base = self.settings.public_app_base_url.rstrip("/")
        if conversation_id is None:
            return f"{base}/chat"
        return f"{base}/chat/{conversation_id}"

    async def _ensure_conversation(self, account: AccountRecord, peer_id: str) -> UUID:
        """保证当前空间有一条微信对话。

        Args:
            account: 当前账号。
            peer_id: 微信用户。

        Returns:
            对话标识。
        """
        link = await self.repo.get_peer(account.id, account.workspace_id, peer_id)
        if link is not None and link.conversation_id is not None:
            return link.conversation_id
        conversation_id = await self.chat.create_conversation(
            ResourceScope(account.user_id, account.workspace_id), "微信对话"
        )
        await self.repo.set_conversation(account.id, account.workspace_id, peer_id, conversation_id)
        return conversation_id


def _match_agent(agents: list[tuple[UUID, str]], argument: str) -> tuple[UUID, str] | str:
    """按名称匹配 Agent。

    Args:
        agents: 可见 Agent。
        argument: 用户输入的名称。

    Returns:
        唯一匹配，或要回复给用户的说明。
    """
    exact = [item for item in agents if item[1] == argument]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return "有多个同名 Agent，请在网页里切换。"
    folded = [item for item in agents if item[1].casefold() == argument.casefold()]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        return "有多个同名 Agent，请在网页里切换。"
    return "没有找到这个 Agent。发送 /agent 查看列表。"


def _final_text(snapshot: RunSnapshot | None) -> str:
    """把运行终态收成一条微信文本。

    Args:
        snapshot: 最新快照。

    Returns:
        发给用户的正文。
    """
    if snapshot is None or snapshot.run.status in {"pending", "running"}:
        return "回答还在生成，请稍后在网页查看。"
    if snapshot.run.status == "completed":
        return snapshot.message.content.strip() or "没有生成内容。"
    if snapshot.run.status == "failed":
        return snapshot.run.error_message or "生成失败，请在网页查看。"
    return snapshot.message.content.strip() or "已取消。"
