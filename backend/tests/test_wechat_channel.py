"""微信渠道：扫码确认、去重、-14、24 小时窗口、审批回复和越权切空间。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.channels.crypto import ChannelCrypto
from app.channels.handler import InboundHandler
from app.channels.ilink import ILinkMessage, QrCode, QrStatus, UpdatesPage
from app.channels.records import AccountRecord, ApprovalTicket, BindSessionRecord, PeerLink, RunDelivery
from app.channels.runtime import TOKEN_RETRY_SECONDS, ChannelRuntime, poll_wait_seconds
from app.channels.service import ChannelService
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.auth import ResourceScope
from app.schemas.chat import MessageView, RunSnapshot, RunView
from app.services.approvals import ApprovalNotice


class MemoryStore:
    """测试用的渠道存储，覆盖绑定和入站这两条路径。"""

    def __init__(self, crypto: ChannelCrypto) -> None:
        """准备空表。

        Args:
            crypto: 与服务相同的加密器，用来回读 token。
        """
        self.crypto = crypto
        self.sessions: dict[UUID, BindSessionRecord] = {}
        self.accounts: dict[UUID, AccountRecord] = {}
        self.ciphertext: dict[UUID, str] = {}
        self.seen: set[tuple[UUID, str]] = set()
        self.peers: dict[tuple[UUID, UUID, str], PeerLink] = {}
        self.outbounds: list[tuple[str, str | None, UUID | None, str]] = []
        self.codes: dict[tuple[UUID, str], ApprovalTicket] = {}
        self.workspaces: list[UUID] = []
        self.deliveries: dict[UUID, object] = {}

    async def create_bind_session(
        self, user_id: UUID, workspace_id: UUID, qrcode: str, image: str, baseurl: str
    ) -> BindSessionRecord:
        """保存二维码会话。"""
        session = BindSessionRecord(uuid4(), user_id, workspace_id, qrcode, image, baseurl, "pending", 0, None)
        self.sessions[session.id] = session
        return session

    async def get_bind_session(self, user_id: UUID, session_id: UUID) -> BindSessionRecord:
        """读取会话。"""
        session = self.sessions[session_id]
        assert session.user_id == user_id
        return session

    async def confirm_bind(
        self, session_id: UUID, user_id: UUID, *, bot_id: str, ilink_user_id: str, baseurl: str, ciphertext: str
    ) -> AccountRecord:
        """确认后保存密文，并让后续读取能解密。"""
        session = self.sessions[session_id]
        account = AccountRecord(
            id=uuid4(),
            user_id=user_id,
            workspace_id=session.workspace_id,
            channel_type="ilink",
            ilink_bot_id=bot_id,
            ilink_user_id=ilink_user_id,
            baseurl=baseurl,
            updates_cursor="",
            status="active",
            paused_until=None,
            created_at=datetime.now(UTC),
            token=self.crypto.decrypt(ciphertext),
        )
        self.accounts[account.id] = account
        self.ciphertext[account.id] = ciphertext
        session.status = "confirmed"
        session.account_id = account.id
        return account

    async def get_account(self, account_id: UUID) -> AccountRecord | None:
        """读取账号。"""
        return self.accounts.get(account_id)

    async def mark_paused(self, account_id: UUID, until: datetime) -> None:
        """记录 -14 暂停。"""
        account = self.accounts[account_id]
        account.status = "paused"
        account.paused_until = until

    async def mark_active(self, account_id: UUID) -> None:
        """暂停结束后恢复为可轮询。"""
        account = self.accounts[account_id]
        account.status = "active"
        account.paused_until = None

    async def save_cursor(self, account_id: UUID, cursor: str) -> None:
        """保存游标。"""
        self.accounts[account_id].updates_cursor = cursor

    async def record_inbound(
        self, account_id: UUID, message_id: str, peer_id: str, status: str, reason: str | None, excerpt: str
    ) -> bool:
        """按 message_id 去重。"""
        del peer_id, status, reason, excerpt
        if not message_id:
            return False
        key = (account_id, message_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    async def touch_inbound(
        self, account_id: UUID, workspace_id: UUID, peer_id: str, context_token: str, at: datetime
    ) -> PeerLink:
        """刷新窗口。"""
        current = self.peers.get((account_id, workspace_id, peer_id))
        link = PeerLink(
            account_id,
            workspace_id,
            peer_id,
            None if current is None else current.conversation_id,
            context_token or ("" if current is None else current.context_token),
            at,
        )
        self.peers[(account_id, workspace_id, peer_id)] = link
        return link

    async def get_peer(self, account_id: UUID, workspace_id: UUID, peer_id: str) -> PeerLink | None:
        """读取对端。"""
        return self.peers.get((account_id, workspace_id, peer_id))

    async def set_conversation(
        self, account_id: UUID, workspace_id: UUID, peer_id: str, conversation_id: UUID
    ) -> PeerLink:
        """绑定对话。"""
        current = self.peers.get((account_id, workspace_id, peer_id))
        link = PeerLink(
            account_id,
            workspace_id,
            peer_id,
            conversation_id,
            "" if current is None else current.context_token,
            None if current is None else current.last_inbound_at,
        )
        self.peers[(account_id, workspace_id, peer_id)] = link
        return link

    async def set_workspace(self, account_id: UUID, workspace_id: UUID) -> None:
        """记录空间切换。"""
        del account_id
        self.workspaces.append(workspace_id)

    async def latest_peer(self, account_id: UUID) -> PeerLink | None:
        """取最近入站。"""
        rows = [item for item in self.peers.values() if item.account_id == account_id and item.last_inbound_at]
        if not rows:
            return None
        return max(rows, key=lambda item: item.last_inbound_at or datetime.min.replace(tzinfo=UTC))

    async def log_outbound(
        self, account_id: UUID, peer_id: str, status: str, reason: str | None, run_id: UUID | None, excerpt: str
    ) -> None:
        """记录出站。"""
        del account_id, peer_id
        self.outbounds.append((status, reason, run_id, excerpt))

    async def outbound_exists(self, run_id: UUID) -> bool:
        """是否已经为该运行出站。"""
        return any(item[2] == run_id for item in self.outbounds)

    async def find_approval_code(self, account_id: UUID, code: str) -> ApprovalTicket | None:
        """查找确认码。"""
        return self.codes.get((account_id, code))

    async def consume_approval_code(self, account_id: UUID, code: str) -> None:
        """用掉确认码。"""
        self.codes.pop((account_id, code), None)

    async def account_for_user(self, user_id: UUID) -> AccountRecord | None:
        """按用户查找未解绑账号。"""
        for account in self.accounts.values():
            if account.user_id == user_id and account.status != "unbound":
                return account
        return None

    async def run_delivery(self, run_id: UUID):  # type: ignore[no-untyped-def]
        """返回测试预设的运行投递材料。"""
        return self.deliveries.get(run_id)

    async def issue_approval_code(self, account_id: UUID, notice: object) -> str:
        """签发固定短码。"""
        from app.services.approvals import ApprovalNotice

        assert isinstance(notice, ApprovalNotice)
        self.codes[(account_id, "ZX23")] = ApprovalTicket(
            "ZX23", account_id, notice.approval_id, notice.run_id, notice.workspace_id
        )
        return "ZX23"

    async def list_user_accounts(self, user_id: UUID) -> list[AccountRecord]:
        """列出未解绑账号。"""
        return [item for item in self.accounts.values() if item.user_id == user_id and item.status != "unbound"]

    async def unbind(self, user_id: UUID, account_id: UUID) -> None:
        """标记解绑。"""
        account = self.accounts[account_id]
        assert account.user_id == user_id
        account.status = "unbound"
        account.token = ""

    async def refresh_bind(self, session_id: UUID, user_id: UUID, qrcode: str, image: str) -> BindSessionRecord:
        """换一张二维码。"""
        session = self.sessions[session_id]
        assert session.user_id == user_id
        session.qrcode = qrcode
        session.qrcode_img_content = image
        session.refresh_count += 1
        session.status = "pending"
        return session


class FakeILink:
    """记录发送，并按脚本返回长轮询或扫码结果。"""

    def __init__(self) -> None:
        """准备空的收发记录。"""
        self.sent: list[str] = []
        self.calls = 0
        self.page = UpdatesPage([], "", False, None)
        self.qr_status = QrStatus("wait")

    async def get_updates(self, account: AccountRecord, timeout_seconds: float | None = None) -> UpdatesPage:
        """返回预设页。"""
        del timeout_seconds
        self.calls += 1
        return self.page

    async def send_text(self, account: AccountRecord, to_user_id: str, context_token: str, text: str) -> None:
        """记下正文。"""
        del account, to_user_id, context_token
        self.sent.append(text)

    async def send_typing(self, account: AccountRecord, to_user_id: str, context_token: str, active: bool) -> None:
        """输入状态在这些测试里不断言。"""
        del account, to_user_id, context_token, active

    async def get_bot_qrcode(self, baseurl: str) -> QrCode:
        """返回固定二维码。"""
        del baseurl
        return QrCode("qr-1", "https://example.test/qr.png")

    async def get_qrcode_status(self, qrcode: str, baseurl: str, verify_code: str | None = None) -> QrStatus:
        """返回预设扫码状态。"""
        del qrcode, baseurl, verify_code
        return self.qr_status


class FakeChat:
    """计数创建的运行，并立刻给出完成快照。"""

    def __init__(self) -> None:
        """从零开始计数。"""
        self.runs = 0

    async def ensure_ready(self) -> None:
        """测试里不申请数据库锁。"""

    async def create_conversation(self, scope: ResourceScope, title: str) -> UUID:
        """返回新对话。"""
        del scope, title
        return uuid4()

    async def create_run(
        self, scope: ResourceScope, conversation_id: UUID, content: str, idempotency_key: str
    ) -> RunSnapshot:
        """记一次运行并返回 pending。"""
        del scope, conversation_id, content, idempotency_key
        self.runs += 1
        return _snapshot(uuid4(), "pending", "")

    def schedule(self, run_id: UUID, request_id: str) -> None:
        """调度在测试里是空操作。"""
        del run_id, request_id

    async def snapshot(self, run_id: UUID) -> RunSnapshot:
        """回复任务一读到快照就是完成。"""
        return _snapshot(run_id, "completed", "模型回复")


class FakeAgents:
    """没有可切换的自定义 Agent。"""

    async def list_agents(self, scope: ResourceScope) -> list[tuple[UUID, str]]:
        """返回空列表。"""
        del scope
        return []

    async def switch(self, scope: ResourceScope, conversation_id: UUID, agent_id: UUID | None) -> None:
        """测试不切换。"""
        del scope, conversation_id, agent_id


class FakeMembers:
    """按构造参数决定空间是否允许进入。"""

    def __init__(self, *, found: bool, allowed: bool) -> None:
        """保存检查结果。

        Args:
            found: 空间是否存在。
            allowed: 用户是否是成员。
        """
        self.found = found
        self.allowed = allowed

    async def resolve(self, user_id: UUID, slug: str):  # type: ignore[no-untyped-def]
        """返回预设资格。"""
        del user_id, slug
        from app.channels.records import WorkspaceGate

        return WorkspaceGate(self.found, self.allowed, uuid4(), "其他空间")


class FakeApprovals:
    """记录审批决定，或抛出权限错误。"""

    def __init__(self, error: ApplicationError | None = None) -> None:
        """准备调用记录。

        Args:
            error: 若设置，则 decide 抛出该错误。
        """
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    async def decide(
        self, scope: ResourceScope, run_id: UUID, approval_id: UUID, decision: str, comment: str | None
    ) -> str:
        """记录决定。"""
        del scope, run_id, approval_id
        if self.error is not None:
            raise self.error
        self.calls.append((decision, comment))
        return "approved" if decision == "approve" else "rejected"


def _settings(**overrides: object) -> Settings:
    """构造不读 .env 的配置。

    Args:
        overrides: 覆盖字段。

    Returns:
        测试配置。
    """
    payload: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "task_scheduler_enabled": False,
        "schedule_enabled": False,
        "knowledge_enabled": False,
        "channel_token_key": SecretStr(Fernet.generate_key().decode()),
    }
    payload.update(overrides)
    return Settings(**payload)  # type: ignore[arg-type]


def _account(settings_user: UUID | None = None) -> AccountRecord:
    """构造绑定账号。

    Args:
        settings_user: 可选用户标识。

    Returns:
        活跃账号。
    """
    return AccountRecord(
        id=uuid4(),
        user_id=settings_user or uuid4(),
        workspace_id=uuid4(),
        channel_type="ilink",
        ilink_bot_id="bot-1",
        ilink_user_id="wx-user",
        baseurl="https://ilinkai.weixin.qq.com",
        updates_cursor="",
        status="active",
        paused_until=None,
        created_at=datetime.now(UTC),
        token="bot-token",
    )


def _message(text: str, message_id: str = "100", state: int | None = 2) -> ILinkMessage:
    """构造绑定用户发来的文本。

    Args:
        text: 正文。
        message_id: 消息 id。
        state: 消息状态。

    Returns:
        入站消息。
    """
    return ILinkMessage(message_id, "wx-user", "bot-1", "", "ctx", text, 1, state)


def _snapshot(run_id: UUID, status: str, content: str) -> RunSnapshot:
    """构造运行快照。

    Args:
        run_id: 运行标识。
        status: 运行状态。
        content: 助手正文。

    Returns:
        快照。
    """
    now = datetime.now(UTC)
    return RunSnapshot(
        run=RunView(
            id=run_id,
            conversation_id=uuid4(),
            status=status,  # type: ignore[arg-type]
            model="test",
            input_sequence=1,
            error_code=None,
            error_message=None,
            cancel_requested_at=None,
            started_at=now,
            finished_at=None if status in {"pending", "running"} else now,
            created_at=now,
        ),
        message=MessageView(
            id=uuid4(),
            conversation_id=uuid4(),
            run_id=run_id,
            sequence=2,
            role="assistant",
            content=content,
            status="completed",
            created_at=now,
            updated_at=now,
        ),
    )


def _handler(
    store: MemoryStore, ilink: FakeILink, *, allowed: bool = True, approvals: FakeApprovals | None = None
) -> tuple[InboundHandler, FakeChat]:
    """组装入站处理器。

    Args:
        store: 内存存储。
        ilink: 假的 iLink。
        allowed: 空间成员检查是否通过。
        approvals: 审批替身。

    Returns:
        处理器和聊天替身。
    """
    chat = FakeChat()
    handler = InboundHandler(
        _settings(),
        store,  # type: ignore[arg-type]
        ilink,  # type: ignore[arg-type]
        chat,
        FakeAgents(),
        FakeMembers(found=True, allowed=allowed),
        approvals or FakeApprovals(),
    )
    return handler, chat


@pytest.mark.asyncio
async def test_qr_bind_confirm_stores_encrypted_token() -> None:
    """扫码确认后只保存密文，接口响应里没有 bot_token。"""
    settings = _settings()
    crypto = ChannelCrypto(settings)
    store = MemoryStore(crypto)
    ilink = FakeILink()
    ilink.qr_status = QrStatus(
        status="confirmed",
        bot_token="plain-token",
        ilink_bot_id="bot-1",
        baseurl="https://ilinkai.weixin.qq.com",
        ilink_user_id="wx-user",
    )
    service = ChannelService(store, ilink, crypto, settings)  # type: ignore[arg-type]
    scope = ResourceScope(uuid4(), uuid4())
    started = await service.start_bind(scope)
    confirmed = await service.poll_bind(scope, started.session_id)
    assert confirmed.status == "confirmed"
    assert confirmed.ilink_user_id == "wx-user"
    assert "plain-token" not in confirmed.model_dump().values()
    assert confirmed.account_id is not None
    assert store.ciphertext[confirmed.account_id] != "plain-token"
    assert crypto.decrypt(store.ciphertext[confirmed.account_id]) == "plain-token"


@pytest.mark.asyncio
async def test_duplicate_and_streaming_partial_do_not_run_twice() -> None:
    """相同 message_id 只跑一次；生成中的增量不占去重位。"""
    store = MemoryStore(ChannelCrypto(_settings()))
    ilink = FakeILink()
    handler, chat = _handler(store, ilink)
    account = _account()
    partial = _message("你好", state=1)
    final = _message("你好", state=2)
    await handler.handle(account, partial)
    await handler.handle(account, final)
    await handler.handle(account, final)
    await handler.drain()
    assert chat.runs == 1
    assert ilink.sent == ["模型回复"]


@pytest.mark.asyncio
async def test_session_expired_pauses_polling_for_about_an_hour() -> None:
    """getUpdates 返回 -14 时暂停约一小时，暂停期内不再请求。"""
    settings = _settings()
    crypto = ChannelCrypto(settings)
    store = MemoryStore(crypto)
    account = _account()
    store.accounts[account.id] = account
    ilink = FakeILink()
    ilink.page = UpdatesPage([], "", True, None)
    runtime = ChannelRuntime(
        MagicMock(),
        settings,
        crypto,
        ilink,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    await runtime.poll_once(account)
    assert account.paused_until is not None
    assert account.paused_until - datetime.now(UTC) > timedelta(seconds=3500)
    await runtime.poll_once(account)
    assert ilink.calls == 1


@pytest.mark.asyncio
async def test_proactive_push_respects_24h_window() -> None:
    """超过 24 小时不发送，只记录 outside_window；窗口内才发送。"""
    store = MemoryStore(ChannelCrypto(_settings()))
    ilink = FakeILink()
    handler, _chat = _handler(store, ilink)
    account = _account()
    stale = datetime.now(UTC) - timedelta(hours=25)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    store.peers[(account.id, account.workspace_id, "wx-user")] = PeerLink(
        account.id, account.workspace_id, "wx-user", uuid4(), "ctx", stale
    )
    first = uuid4()
    await handler.push_proactive(account, "【定时问答】\n结果", run_id=first)
    assert ilink.sent == []
    assert store.outbounds[-1][0] == "skipped"
    assert store.outbounds[-1][1] == "outside_window"
    store.peers[(account.id, account.workspace_id, "wx-user")] = PeerLink(
        account.id, account.workspace_id, "wx-user", uuid4(), "ctx", fresh
    )
    await handler.push_proactive(account, "【定时问答】\n结果", run_id=uuid4())
    assert ilink.sent == ["【定时问答】\n结果"]


@pytest.mark.asyncio
async def test_approval_reply_uses_existing_decide() -> None:
    """同意和拒绝短码会带着原因调用审批服务。"""
    store = MemoryStore(ChannelCrypto(_settings()))
    ilink = FakeILink()
    approvals = FakeApprovals()
    handler, _chat = _handler(store, ilink, approvals=approvals)
    account = _account()
    store.codes[(account.id, "AB23")] = ApprovalTicket("AB23", account.id, uuid4(), uuid4(), account.workspace_id)
    store.codes[(account.id, "CD34")] = ApprovalTicket("CD34", account.id, uuid4(), uuid4(), account.workspace_id)
    await handler.handle(account, _message("同意 AB23", "m1"))
    await handler.handle(account, _message("拒绝 CD34 预算不够", "m2"))
    assert approvals.calls == [("approve", None), ("reject", "预算不够")]
    assert "已同意。" in ilink.sent
    assert "已拒绝。" in ilink.sent


@pytest.mark.asyncio
async def test_unauthorized_workspace_switch_does_not_change_binding() -> None:
    """不是目标空间成员时保持原空间，并说明原因。"""
    store = MemoryStore(ChannelCrypto(_settings()))
    ilink = FakeILink()
    handler, _chat = _handler(store, ilink, allowed=False)
    account = _account()
    original = account.workspace_id
    await handler.handle(account, _message("/ws other-space", "m-ws"))
    assert account.workspace_id == original
    assert store.workspaces == []
    assert any("不是该空间的成员" in item for item in ilink.sent)


@pytest.mark.asyncio
async def test_commands_new_agent_and_missing_approval_code() -> None:
    """新对话、Agent 列表和无效确认码都只回复说明，不创建问答运行。"""
    store = MemoryStore(ChannelCrypto(_settings()))
    ilink = FakeILink()
    handler, chat = _handler(store, ilink)
    account = _account()
    await handler.handle(account, _message("/new", "n1"))
    await handler.handle(account, _message("/agent", "n2"))
    await handler.handle(account, _message("同意 ZZ99", "n3"))
    await handler.handle(account, _message("", "n4"))
    assert chat.runs == 0
    assert "已开始新对话。" in ilink.sent
    assert any("序列助手" in item for item in ilink.sent)
    assert any("没有找到这个确认码" in item for item in ilink.sent)
    assert any("只能处理文字" in item for item in ilink.sent)


@pytest.mark.asyncio
async def test_schedule_result_and_approval_push_use_the_window() -> None:
    """定时结果在窗口内推送；审批在窗口外只记原因。"""
    settings = _settings()
    crypto = ChannelCrypto(settings)
    store = MemoryStore(crypto)
    ilink = FakeILink()
    handler, _chat = _handler(store, ilink)
    account = _account()
    store.accounts[account.id] = account
    runtime = ChannelRuntime(
        MagicMock(),
        settings,
        crypto,
        ilink,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    runtime.handler = handler
    stale_run = uuid4()
    fresh_run = uuid4()
    store.deliveries[stale_run] = RunDelivery(
        stale_run, account.user_id, account.workspace_id, "completed", "晨报正文", None, "schedule_abc"
    )
    store.deliveries[fresh_run] = RunDelivery(
        fresh_run, account.user_id, account.workspace_id, "completed", "晨报正文", None, "schedule_abc"
    )
    store.peers[(account.id, account.workspace_id, "wx-user")] = PeerLink(
        account.id, account.workspace_id, "wx-user", uuid4(), "ctx", datetime.now(UTC) - timedelta(hours=30)
    )
    await runtime.on_run_finished(stale_run)
    assert ilink.sent == []
    assert store.outbounds[-1][1] == "outside_window"
    store.peers[(account.id, account.workspace_id, "wx-user")] = PeerLink(
        account.id, account.workspace_id, "wx-user", uuid4(), "ctx", datetime.now(UTC) - timedelta(minutes=5)
    )
    await runtime.on_run_finished(fresh_run)
    assert ilink.sent == ["【定时问答】\n晨报正文"]
    handler.direct_runs.add(fresh_run)
    await runtime.on_run_finished(fresh_run)
    assert ilink.sent == ["【定时问答】\n晨报正文"]
    store.peers[(account.id, account.workspace_id, "wx-user")] = PeerLink(
        account.id, account.workspace_id, "wx-user", uuid4(), "ctx", datetime.now(UTC) - timedelta(hours=30)
    )
    notice = ApprovalNotice(
        uuid4(), uuid4(), account.workspace_id, account.user_id, "创建定时任务", "{}", datetime.now(UTC)
    )
    await runtime.on_approval(notice)
    assert "同意" not in "".join(ilink.sent)
    assert store.outbounds[-1][1] == "outside_window"


@pytest.mark.asyncio
async def test_bind_status_edges_and_unbind() -> None:
    """未配置密钥、需要数字验证码、过期换码和解绑。"""
    empty = _settings(channel_token_key=SecretStr(""))
    disabled = ChannelService(MemoryStore(ChannelCrypto(empty)), FakeILink(), ChannelCrypto(empty), empty)  # type: ignore[arg-type]
    with pytest.raises(ApplicationError) as caught:
        await disabled.list_accounts(ResourceScope(uuid4(), uuid4()))
    assert caught.value.code == "CHANNEL_NOT_CONFIGURED"

    settings = _settings()
    crypto = ChannelCrypto(settings)
    store = MemoryStore(crypto)
    ilink = FakeILink()
    service = ChannelService(store, ilink, crypto, settings)  # type: ignore[arg-type]
    scope = ResourceScope(uuid4(), uuid4())
    ilink.qr_status = QrStatus("need_verifycode")
    started = await service.start_bind(scope)
    waiting = await service.poll_bind(scope, started.session_id)
    assert "数字" in waiting.message
    ilink.qr_status = QrStatus("expired")
    refreshed = await service.poll_bind(scope, started.session_id)
    assert refreshed.status == "pending"
    assert refreshed.qrcode_img_content.startswith("https://")
    account = _account(scope.user_id)
    account.workspace_id = scope.workspace_id
    store.accounts[account.id] = account
    listed = await service.list_accounts(scope)
    assert listed[0].id == account.id
    assert not hasattr(listed[0], "token")
    await service.unbind(scope, account.id)
    assert store.accounts[account.id].status == "unbound"


def test_missing_token_waits_a_minute() -> None:
    """解不开凭证时按分钟重试，避免空转打数据库。"""
    account = _account()
    account.token = ""
    assert poll_wait_seconds(account, datetime.now(UTC)) == TOKEN_RETRY_SECONDS


@pytest.mark.asyncio
async def test_pause_status_returns_to_active_after_the_hour() -> None:
    """暂停时间一过，设置页看到的状态回到已连接。"""
    settings = _settings()
    store = MemoryStore(ChannelCrypto(settings))
    ilink = FakeILink()
    ilink.page = UpdatesPage([], "cursor-next", False, None)
    account = _account()
    account.status = "paused"
    account.paused_until = datetime.now(UTC) - timedelta(seconds=5)
    store.accounts[account.id] = account
    runtime = ChannelRuntime(
        MagicMock(),
        settings,
        ChannelCrypto(settings),
        ilink,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    await runtime.poll_once(account)
    assert account.status == "active"
    assert account.paused_until is None
    assert account.updates_cursor == "cursor-next"


@pytest.mark.asyncio
async def test_cursor_advances_only_after_the_batch_is_handled() -> None:
    """处理中途失败时不推进游标，下一次长轮询还能看到这批消息。"""
    settings = _settings()
    store = MemoryStore(ChannelCrypto(settings))
    ilink = FakeILink()
    ilink.page = UpdatesPage([_message("你好", "42")], "cursor-next", False, None)
    account = _account()
    store.accounts[account.id] = account
    runtime = ChannelRuntime(
        MagicMock(),
        settings,
        ChannelCrypto(settings),
        ilink,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    async def fail(current: AccountRecord, message: ILinkMessage) -> None:
        del current, message
        raise RuntimeError("处理失败")

    runtime.handler.handle = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await runtime.poll_once(account)
    assert account.updates_cursor == ""
    assert ilink.calls == 1


@pytest.mark.asyncio
async def test_messages_without_id_each_create_a_run() -> None:
    """没有 message_id 的两条文本不会共用同一个幂等键。"""
    store = MemoryStore(ChannelCrypto(_settings()))
    ilink = FakeILink()
    handler, chat = _handler(store, ilink)
    account = _account()
    await handler.handle(account, _message("第一条", ""))
    await handler.handle(account, _message("第二条", ""))
    await handler.drain()
    assert chat.runs == 2
