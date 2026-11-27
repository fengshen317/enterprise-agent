"""把微信入站消息变成对话运行、指令或审批决定。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from app.channels.capabilities import (
    ChannelCapabilities,
    bind_instructions,
    capability_for,
    conversation_title,
    delivery_skip_reason,
    reminder_due,
    reminder_text,
    welcome_text,
)
from app.channels.commands import chunk_text, parse_inbound
from app.channels.ilink import ILinkMessage
from app.channels.inbound import InboundEvent
from app.channels.records import AccountRecord, AppRecord, ApprovalTicket, WorkspaceGate
from app.channels.repository import ChannelStore
from app.channels.send_error import ChannelSendError
from app.channels.wechat_text import wechat_reply
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope
from app.schemas.chat import RunSnapshot
from app.services.system_logs import emit_system_log, system_log_context

if TYPE_CHECKING:
    from app.services.system_logs import SystemLogRecorder

logger = logging.getLogger(__name__)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_WEB_ONLY_RISKS = frozenset({"delete", "external"})
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class Outbound(Protocol):
    """微信客户端或渠道总线都要能发文本和输入状态。"""

    async def send_text(self, account: AccountRecord, to_user_id: str, context_token: str, text: str) -> None:
        """发送一段文本。"""

    async def send_typing(self, account: AccountRecord, to_user_id: str, context_token: str, active: bool) -> None:
        """打开或关闭输入状态。"""


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


def _idempotency_key(channel_type: str, message_id: str) -> str:
    """用渠道和消息号生成问答幂等键。微信保持原来的 wx 前缀。

    Args:
        channel_type: 渠道类型。
        message_id: 平台消息号。

    Returns:
        符合运行幂等规则的键。
    """
    if channel_type == "ilink":
        return wechat_idempotency_key(message_id)
    prefix = {"feishu": "fs", "dingtalk": "dt", "qq": "qq"}.get(channel_type, "ch")
    raw = f"{prefix}{message_id}"
    if _IDEMPOTENCY.fullmatch(raw) and len(raw) <= 100:
        return raw
    return prefix + hashlib.sha256(message_id.encode()).hexdigest()[:40]


def fallback_message_id(message: ILinkMessage) -> str:
    """没有 message_id 时用稳定字段做摘要，让同一页重放仍能去重。

    Args:
        message: 入站消息。

    Returns:
        以 local- 开头的编号。
    """
    parts = [
        message.from_user_id,
        message.to_user_id,
        message.client_id,
        "" if message.seq is None else str(message.seq),
        "" if message.create_time_ms is None else str(message.create_time_ms),
        message.context_token,
        "" if message.message_type is None else str(message.message_type),
        "" if message.message_state is None else str(message.message_state),
        message.text,
    ]
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]
    return f"local-{digest}"


class InboundHandler:
    """处理一个账号收到的文本。长轮询本身不等待模型生成。"""

    def __init__(
        self,
        settings: Settings,
        repo: ChannelStore,
        ilink: Outbound,
        chat: ChatPort,
        agents: AgentPort,
        membership: MembershipPort,
        approvals: ApprovalPort,
        *,
        log_recorder: SystemLogRecorder | None = None,
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
            log_recorder: 应用的脱敏诊断记录器。
        """
        self.settings = settings
        self.repo = repo
        self.ilink = ilink
        self.chat = chat
        self.agents = agents
        self.membership = membership
        self.approvals = approvals
        self.log_recorder = log_recorder
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
            reason = "not_user" if account.channel_type == "ilink" else "not_text"
            await self.repo.record_inbound(
                account.id, message.message_id, message.from_user_id, "skipped", reason, message.text[:500]
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
        # 没有 message_id 时用内容摘要做稳定编号。游标未推进而整页重放时，不会再开一次运行。
        if not message.message_id:
            message = ILinkMessage(
                fallback_message_id(message),
                message.from_user_id,
                message.to_user_id,
                message.group_id,
                message.context_token,
                message.text,
                message.message_type,
                message.message_state,
                message.client_id,
                message.seq,
                message.create_time_ms,
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
        greeting = welcome_text(account.channel_type)
        if account.welcome_pending and await self.reply(account, message, greeting):
            await self.repo.clear_welcome(account.id)
            account.welcome_pending = False
            if command.kind == "help":
                return
        if command.kind == "help":
            await self.reply(account, message, greeting)
        elif command.kind == "bind":
            if account.channel_type == "ilink":
                await self.reply(account, message, "微信在网页里扫码绑定，不需要绑定码。")
            else:
                await self.reply(account, message, "当前会话已经绑定。如需更换，请先在网页解除绑定，再发送新的绑定码。")
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

    async def handle_event(self, account: AccountRecord, event: InboundEvent) -> None:
        """把企业渠道的归一化消息交给和微信相同的指令与问答流程。

        Args:
            account: 已经绑定的员工。平台用户必须等于绑定上的 ilink_user_id。
            event: 单聊或群消息。
        """
        message = ILinkMessage(
            event.message_id,
            event.peer_id,
            "",
            "group" if event.is_group else "",
            event.reply_message_id,
            event.text,
            1 if event.kind == "text" else 2,
            None,
        )
        await self.handle(account, message)

    async def handle_unbound(self, app: AppRecord, event: InboundEvent) -> None:
        """未绑定用户只收到绑定说明。消息记在应用审计上，不进入任何工作台账号。

        Args:
            app: 收到消息的企业机器人。
            event: 归一化后的入站消息。
        """
        if event.is_group:
            await self.repo.record_app_event(
                app.id,
                event.message_id,
                event.peer_id,
                "skipped",
                "group",
                event.text[:500],
            )
            return
        if event.kind != "text":
            await self.repo.record_app_event(
                app.id,
                event.message_id,
                event.peer_id,
                "skipped",
                "not_text",
                event.text[:500],
            )
            await self._send_unbound(app, event.peer_id, "目前只能处理文字消息。", event.reply_message_id)
            return
        inserted = await self.repo.record_app_event(
            app.id,
            event.message_id,
            event.peer_id,
            "received",
            None,
            event.text[:500],
        )
        if not inserted:
            return
        command = parse_inbound(event.text.strip())
        if command.kind == "bind" and command.argument:
            try:
                account = await self.repo.redeem_bind_code(app.channel_type, event.peer_id, command.argument)
            except ApplicationError as exc:
                await self._send_unbound(app, event.peer_id, exc.message, event.reply_message_id)
                return
            if account is None:
                await self._send_unbound(
                    app,
                    event.peer_id,
                    "绑定码不正确或已过期。请在工作台重新生成。",
                    event.reply_message_id,
                )
                return
            # /bind 本身就是一次单聊，钉钉之后的主动推送要能看见这次入站。
            await self.repo.touch_inbound(
                account.id,
                account.workspace_id,
                event.peer_id,
                event.reply_message_id,
                datetime.now(UTC),
            )
            if await self._send_unbound(app, event.peer_id, welcome_text(app.channel_type), event.reply_message_id):
                await self.repo.clear_welcome(account.id)
            return
        await self._send_unbound(app, event.peer_id, bind_instructions(app.channel_type), event.reply_message_id)

    async def _send_unbound(self, app: AppRecord, peer_id: str, text: str, msg_id: str = "") -> bool:
        """通过应用凭证回复还没绑定的用户。

        Args:
            app: 企业应用。
            peer_id: 平台用户。
            text: 说明。
            msg_id: 刚收到的消息号。QQ 用它做被动回复。

        Returns:
            是否发送成功。
        """
        sender = getattr(self.ilink, "send_app_text", None)
        if sender is None:
            return False
        try:
            await sender(app, peer_id, text, msg_id=msg_id)
        except ChannelSendError:
            return False
        except Exception:
            logger.warning("channel_unbound_reply_failed", extra={"app_id": str(app.id)})
            return False
        return True

    def _caps(self, account: AccountRecord) -> ChannelCapabilities:
        """优先用出站总线声明的能力；微信测试替身没有这个方法时按渠道默认值。

        Args:
            account: 当前绑定。

        Returns:
            渠道能力。
        """
        method = getattr(self.ilink, "capabilities", None)
        if callable(method):
            found = method(account.channel_type)
            if isinstance(found, ChannelCapabilities):
                return found
        return capability_for(account.channel_type)

    async def push_proactive(
        self,
        account: AccountRecord,
        text: str,
        *,
        run_id: UUID | None,
        workspace_id: UUID | None = None,
        conversation_id: UUID | None = None,
    ) -> None:
        """在 24 小时窗口内主动推送。窗口外只记原因，网页里的回答保留。

        Args:
            account: 接收账号。
            text: 已带上来源前缀的正文。
            run_id: 关联运行。为空时不去重。
            workspace_id: 推送来源空间；与 conversation_id 同时提供时切换后续入站。
            conversation_id: 推送来源对话。
        """
        if run_id is not None and await self.repo.outbound_exists(run_id, account.id):
            return
        peer = await self.repo.latest_peer(account.id)
        reason = delivery_skip_reason(self._caps(account), peer, datetime.now(UTC), self.settings)
        if reason is not None or (peer is None and self._caps(account).proactive_uses_reply_window):
            await self.repo.log_outbound(
                account.id,
                peer.peer_id if peer is not None else "",
                "skipped",
                reason or "no_inbound",
                run_id,
                text[:500],
            )
            prefix = "WECHAT" if account.channel_type == "ilink" else account.channel_type.upper()
            self._record_delivery(
                workspace_id or account.workspace_id,
                run_id,
                "warning",
                f"{prefix}_{(reason or 'no_inbound').upper()}",
                channel_type=account.channel_type,
            )
            return
        target = peer.peer_id if peer is not None else account.ilink_user_id
        token = peer.context_token if peer is not None else ""
        sent = await self.send_text(account, target, token, text, run_id=run_id, workspace_id=workspace_id)
        if sent and workspace_id is not None and conversation_id is not None:
            # 只有手机实际收到消息后才切换上下文，避免失败推送劫持后续微信问答。
            await self.repo.activate_conversation(account.id, workspace_id, target, conversation_id)
            account.workspace_id = workspace_id

    async def reply(self, account: AccountRecord, message: ILinkMessage, text: str) -> bool:
        """回复刚刚收到的消息，优先用这条消息自己的 context_token。

        Args:
            account: 发送账号。
            message: 入站消息。
            text: 回复正文。

        Returns:
            是否发送成功。
        """
        link = await self.repo.get_peer(account.id, account.workspace_id, message.from_user_id)
        token = message.context_token or (link.context_token if link is not None else "")
        return await self.send_text(account, message.from_user_id, token, text, run_id=None)

    def _record_delivery(
        self,
        workspace_id: UUID,
        run_id: UUID | None,
        outcome: Literal["success", "warning", "error"],
        error_code: str | None = None,
        duration_ms: float | None = None,
        channel_type: str = "ilink",
    ) -> None:
        """按投递来源记录结果，排除渠道身份、令牌和正文。

        Args:
            workspace_id: 投递来源的可信空间。
            run_id: 可关联的对话运行。
            outcome: 实际投递结果。
            error_code: 固定错误码。
            duration_ms: 可用的投递耗时。
            channel_type: 渠道类型。微信继续记在原有事件上。
        """
        channel = "wechat" if channel_type == "ilink" else channel_type
        component = "wechat" if channel == "wechat" else channel_type
        event = "wechat_delivery" if channel == "wechat" else "channel_delivery"
        with system_log_context(recorder=self.log_recorder, workspace_id=workspace_id, run_id=run_id):
            emit_system_log(
                component=component,  # type: ignore[arg-type]
                event=event,
                outcome=outcome,
                error_code=error_code,
                duration_ms=duration_ms,
                details={"channel": channel},
            )

    async def send_text(
        self,
        account: AccountRecord,
        to_user_id: str,
        context_token: str,
        text: str,
        *,
        run_id: UUID | None,
        workspace_id: UUID | None = None,
    ) -> bool:
        """分段发送并写出站日志。

        Args:
            account: 发送账号。
            to_user_id: 接收用户。
            context_token: 会话令牌。
            text: 完整正文。
            run_id: 关联运行。
            workspace_id: 异步投递来源的空间快照，默认使用当前渠道空间。

        Returns:
            是否发送成功。失败时不把一次性提醒标成已发送。
        """
        started = monotonic()
        diagnostic_workspace = workspace_id or account.workspace_id
        limit = (
            self.settings.ilink_message_chunk_characters
            if account.channel_type == "ilink"
            else self._caps(account).max_text_chars
        )
        chunks = chunk_text(text, limit)
        if not chunks:
            return False
        try:
            for chunk in chunks:
                await self.ilink.send_text(account, to_user_id, context_token, chunk)
        except ChannelSendError as exc:
            logger.warning(
                "channel_send_rejected",
                extra={"account_id": str(account.id), "run_id": str(run_id or ""), "reason": exc.reason},
            )
            await self.repo.log_outbound(account.id, to_user_id, "failed", exc.reason, run_id, text[:500])
            self._record_delivery(
                diagnostic_workspace,
                run_id,
                "error",
                f"{account.channel_type.upper()}_{exc.reason.upper()}",
                round((monotonic() - started) * 1000, 2),
                channel_type=account.channel_type,
            )
            return False
        except Exception:
            logger.warning("channel_send_failed", extra={"account_id": str(account.id), "run_id": str(run_id or "")})
            await self.repo.log_outbound(account.id, to_user_id, "failed", "send_failed", run_id, text[:500])
            failed = "WECHAT_DELIVERY_FAILED" if account.channel_type == "ilink" else "CHANNEL_DELIVERY_FAILED"
            self._record_delivery(
                diagnostic_workspace,
                run_id,
                "error",
                failed,
                round((monotonic() - started) * 1000, 2),
                channel_type=account.channel_type,
            )
            return False
        await self.repo.log_outbound(account.id, to_user_id, "sent", None, run_id, text[:500])
        if run_id is not None:
            self._record_delivery(
                diagnostic_workspace,
                run_id,
                "success",
                duration_ms=round((monotonic() - started) * 1000, 2),
                channel_type=account.channel_type,
            )
        return True

    async def _new(self, account: AccountRecord, message: ILinkMessage) -> None:
        """开启一条新的网页对话并让后续消息进入它。

        Args:
            account: 当前账号。
            message: /new 消息。
        """
        scope = ResourceScope(account.user_id, account.workspace_id)
        try:
            conversation_id = await self.chat.create_conversation(scope, conversation_title(account.channel_type))
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
                scope, conversation_id, text[:20000], _idempotency_key(account.channel_type, message.message_id)
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
        self.chat.schedule(run_id, f"{account.channel_type}-{run_id.hex[:8]}")
        token = message.context_token
        self.spawn(
            self._deliver(
                account, message.from_user_id, token, run_id, snapshot.run.conversation_id, scope.workspace_id
            )
        )

    async def _deliver(
        self,
        account: AccountRecord,
        to_user_id: str,
        context_token: str,
        run_id: UUID,
        conversation_id: UUID,
        workspace_id: UUID,
    ) -> None:
        """等运行结束，把最终正文发回微信。超过阈值时穿插进度。

        Args:
            account: 发送账号。
            to_user_id: 接收用户。
            context_token: 触发这条回复的令牌。
            run_id: 问答运行。
            conversation_id: 网页上打开完整回答的对话。
            workspace_id: 接到该消息时的空间，防止切换空间后诊断串到另一空间。
        """
        page_url = self._page_url(conversation_id, workspace_id)
        stop_progress = asyncio.Event()
        progress = asyncio.create_task(
            self._progress(account, to_user_id, context_token, run_id, stop_progress, page_url)
        )
        try:
            await self.ilink.send_typing(account, to_user_id, context_token, True)
            deadline = monotonic() + self.settings.chat_run_timeout_seconds + 15
            snapshot: RunSnapshot | None = None
            while monotonic() < deadline:
                snapshot = await self.chat.snapshot(run_id)
                if snapshot.run.status not in {"pending", "running"}:
                    break
                await asyncio.sleep(0.4)
            # 先等进度任务退出，再发最终回答，避免「仍在处理」排到正文后面。
            stop_progress.set()
            await asyncio.gather(progress, return_exceptions=True)
            body = wechat_reply(
                _final_text(snapshot),
                preview_characters=self.settings.channel_reply_preview_characters,
                page_url=page_url,
            )
            await self.send_text(account, to_user_id, context_token, body, run_id=run_id, workspace_id=workspace_id)
        finally:
            stop_progress.set()
            if not progress.done():
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)
            self.direct_runs.discard(run_id)
            await self.ilink.send_typing(account, to_user_id, context_token, False)

    async def _progress(
        self,
        account: AccountRecord,
        to_user_id: str,
        context_token: str,
        run_id: UUID,
        stop: asyncio.Event,
        page_url: str,
    ) -> None:
        """运行偏久时发一句进度，相同内容不重复刷屏。

        Args:
            account: 发送账号。
            to_user_id: 接收用户。
            context_token: 当前会话令牌。
            run_id: 问答运行。
            stop: 运行结束后置位，进度任务发完手头的一句就退出。
            page_url: 可实时查看同一条回答的工作台链接。
        """
        if self._caps(account).window_reply_budget is not None:
            # QQ 每条消息只有 4 次被动回复，进度改由「正在输入」表达，不再另发文本。
            try:
                await stop.wait()
            except asyncio.CancelledError:
                raise
            return
        try:
            await asyncio.wait_for(stop.wait(), self.settings.channel_progress_after_seconds)
            return
        except TimeoutError:
            pass
        last = ""
        linked = False
        while not stop.is_set():
            label = await self.chat.activity(run_id)
            if stop.is_set():
                return
            if label and label != last and context_token:
                text = label if linked else f"{label}\n\n实时查看回答：{page_url}"
                if await self.send_text(account, to_user_id, context_token, text, run_id=None):
                    linked = True
                await self.ilink.send_typing(account, to_user_id, context_token, True)
                last = label
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), self.settings.channel_progress_interval_seconds)
            except TimeoutError:
                continue

    async def remind_if_due(self, account: AccountRecord) -> None:
        """窗口还剩一小段时间时提醒一次。已经关闭的窗口不再发。

        Args:
            account: 当前账号。
        """
        peer = await self.repo.latest_peer(account.id)
        inbound = reminder_due(self._caps(account), peer, datetime.now(UTC), self.settings)
        if inbound is None or peer is None:
            return
        sent = await self.send_text(
            account,
            peer.peer_id,
            peer.context_token,
            reminder_text(account.channel_type),
            run_id=None,
        )
        if sent:
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

    def _page_url(self, conversation_id: UUID | None, workspace_id: UUID | None = None) -> str:
        """拼网页地址。没有对话时回到问答首页。

        Args:
            conversation_id: 对话标识。
            workspace_id: 对话所属空间，避免网页沿用另一个已选空间。

        Returns:
            绝对地址。
        """
        base = self.settings.public_app_base_url.rstrip("/")
        if conversation_id is None:
            return f"{base}/chat"
        suffix = f"?workspace_id={workspace_id}" if workspace_id is not None else ""
        return f"{base}/chat/{conversation_id}{suffix}"

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
            ResourceScope(account.user_id, account.workspace_id), conversation_title(account.channel_type)
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
