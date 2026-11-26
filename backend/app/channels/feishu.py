"""飞书自建应用机器人：tenant_access_token、单聊发送，以及官方长连接事件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

import httpx

from app.channels.inbound import InboundEvent
from app.channels.records import AppRecord
from app.channels.send_error import ChannelSendError

logger = logging.getLogger(__name__)
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
# 可用范围、接收人无效、机器人能力未发布，都是配置问题，重试不会自己好。
_CONFIG_CODES = {230002, 230006, 230013, 230027, 230034, 99991663, 99991672}
Dispatch = Callable[[InboundEvent], Awaitable[None]]


def parse_feishu_event(payload: dict[str, Any]) -> InboundEvent | None:
    """把 im.message.receive_v1 收成入站事件。只把 p2p 当成单聊。

    Args:
        payload: 事件体。可以是开放平台的 schema 2.0 字典。

    Returns:
        入站事件；没有消息体时为 None。
    """
    event = _mapping(payload.get("event"))
    message = _mapping(event.get("message"))
    sender = _mapping(event.get("sender"))
    sender_id = _mapping(sender.get("sender_id"))
    if not message:
        return None
    message_type = str(message.get("message_type") or "")
    text = ""
    if message_type == "text":
        text = _text_content(str(message.get("content") or ""))
    header = _mapping(payload.get("header"))
    message_id = str(message.get("message_id") or header.get("event_id") or "")
    return InboundEvent(
        message_id=message_id,
        peer_id=str(sender_id.get("open_id") or ""),
        text=text,
        is_group=str(message.get("chat_type") or "") != "p2p",
        kind="text" if message_type == "text" else "other",
    )


def parse_feishu_card(payload: dict[str, Any]) -> InboundEvent | None:
    """卡片按钮带回同意或拒绝短码，仍走原来的审批口令。

    Args:
        payload: card.action.trigger 事件。

    Returns:
        合成的文本指令；按钮没有确认码时为 None。
    """
    event = _mapping(payload.get("event"))
    action = _mapping(event.get("action"))
    value = _mapping(action.get("value"))
    decision = str(value.get("decision") or "")
    code = str(value.get("code") or "")
    if decision not in {"approve", "reject"} or not code:
        return None
    operator = _mapping(event.get("operator"))
    header = _mapping(payload.get("header"))
    verb = "同意" if decision == "approve" else "拒绝"
    return InboundEvent(
        message_id="card-" + str(header.get("event_id") or code),
        peer_id=str(operator.get("open_id") or ""),
        text=f"{verb} {code}",
        is_group=False,
    )


def approval_card(text: str, *, code: str | None, web_only: bool) -> str:
    """飞书 interactive 卡片。高风险按钮不放进来，正文仍指向网页。

    Args:
        text: 说明。
        code: 写操作确认码。
        web_only: 删除和外发。

    Returns:
        content 字段要求的 JSON 字符串。
    """
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text}]
    if code and not web_only:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    _button("同意", "primary", {"decision": "approve", "code": code}),
                    _button("拒绝", "danger", {"decision": "reject", "code": code}),
                ],
            }
        )
    return json.dumps({"elements": elements}, ensure_ascii=False)


class FeishuClient:
    """飞书开放平台的出站客户端。长连接在独立线程里跑官方 SDK。"""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        """可以注入 HTTP 客户端，测试不必访问公网。

        Args:
            http: 可选的 httpx 客户端。
        """
        self.http = http or httpx.AsyncClient(timeout=20)
        self._owns_http = http is None
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._next_send: dict[str, float] = {}
        self._sessions: dict[str, _FeishuSession] = {}

    async def aclose(self) -> None:
        """关闭自己创建的 HTTP 客户端。"""
        if self._owns_http:
            await self.http.aclose()

    async def send_text(self, app: AppRecord, open_id: str, text: str) -> None:
        """用 tenant_access_token 向 open_id 发文本。

        Args:
            app: 企业应用凭证。
            open_id: 员工在这个应用下的 open_id。
            text: 正文。

        Raises:
            ChannelSendError: 平台拒绝或网络失败。
        """
        await self._throttle(open_id)
        await self._post_message(app, open_id, "text", json.dumps({"text": text}, ensure_ascii=False))

    async def send_card(self, app: AppRecord, open_id: str, card: str) -> None:
        """发送互动卡片。失败时调用方再退回纯文本。

        Args:
            app: 企业应用凭证。
            open_id: 接收人。
            card: 卡片 JSON 字符串。

        Raises:
            ChannelSendError: 平台拒绝。
        """
        await self._throttle(open_id)
        await self._post_message(app, open_id, "interactive", card)

    async def serve(self, app: AppRecord, dispatch: Dispatch) -> None:
        """在守护线程里维持官方长连接。取消本任务时关掉这条连接。

        飞书 SDK 在导入时绑定自己的事件循环，不能和问答循环混用。

        Args:
            app: 企业应用。
            dispatch: 收到单聊或卡片按钮后的处理。
        """
        key = str(app.id)
        session = _FeishuSession()
        self._sessions[key] = session
        loop = asyncio.get_running_loop()
        thread = threading.Thread(
            target=_run_sdk,
            args=(app.external_app_id, app.secret, loop, dispatch, session),
            name=f"feishu-{key[:8]}",
            daemon=True,
        )
        session.thread = thread
        thread.start()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # 只发停止信号，不在问答事件循环里 join。SDK 的 start() 经常不返回，join 会把微信轮询一起卡住。
            _stop_feishu(session)
            logger.info("feishu_connection_task_cancelled", extra={"app_id": key})
            raise

    async def _post_message(self, app: AppRecord, open_id: str, msg_type: str, content: str) -> None:
        """调用 im/v1/messages。同一用户限频约 5 QPS，由 _throttle 提前拉开。"""
        token = await self._tenant_token(app)
        try:
            response = await self.http.post(
                _SEND_URL,
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": open_id, "msg_type": msg_type, "content": content},
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("send_failed", retryable=True) from exc
        if response.status_code == 429:
            raise ChannelSendError("rate_limited", retryable=False)
        if response.status_code >= 500:
            raise ChannelSendError("send_failed", retryable=True)
        if response.status_code >= 400:
            raise ChannelSendError(_feishu_reason(response), retryable=False)
        body = _json_object(response)
        code = body.get("code")
        if code not in (0, None):
            raise ChannelSendError(_reason_from_code(code, str(body.get("msg") or "")), retryable=False)

    async def _tenant_token(self, app: AppRecord) -> str:
        """缓存 tenant_access_token，过期前一分钟刷新。"""
        cached = self._tokens.get(_token_key(app))
        if cached is not None and cached[1] > monotonic() + 60:
            return cached[0]
        try:
            response = await self.http.post(
                _TOKEN_URL,
                json={"app_id": app.external_app_id, "app_secret": app.secret},
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("token_failed", retryable=True) from exc
        body = _json_object(response)
        token = str(body.get("tenant_access_token") or "")
        if response.status_code >= 400 or not token or body.get("code") not in (0, None):
            raise ChannelSendError("token_failed", retryable=False)
        expire = float(body.get("expire") or 7200)
        self._tokens[_token_key(app)] = (token, monotonic() + expire)
        return token

    async def _throttle(self, open_id: str) -> None:
        """同一用户两次发送至少间隔 0.2 秒，避开 5 QPS。"""
        async with self._lock:
            now = monotonic()
            due = self._next_send.get(open_id, 0)
            self._next_send[open_id] = max(now, due) + 0.2
            delay = max(0.0, due - now)
        if delay:
            await asyncio.sleep(delay)


class _FeishuSession:
    """一条飞书长连接的线程和事件循环，取消时用来关掉它。"""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: Any | None = None
        self.stopped = False


def _run_sdk(
    app_id: str,
    secret: str,
    loop: asyncio.AbstractEventLoop,
    dispatch: Dispatch,
    session: _FeishuSession,
) -> None:
    """在线程自己的事件循环里启动飞书长连接。"""
    thread_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(thread_loop)
    session.loop = thread_loop
    import lark_oapi as lark
    import lark_oapi.ws.client as ws_client
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    # SDK 在导入时记下的循环可能属于别的线程。这条连接必须用当前线程的循环。
    ws_client.loop = thread_loop

    def on_message(data: object) -> None:
        if session.stopped:
            return
        event = _event_from_receive(data)
        if event is not None:
            asyncio.run_coroutine_threadsafe(_forward(dispatch, event), loop)

    def on_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        if not session.stopped:
            event = _event_from_card(data)
            if event is not None:
                asyncio.run_coroutine_threadsafe(_forward(dispatch, event), loop)
        response = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = "已收到"
        response.toast = toast
        return response

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card)
        .build()
    )
    if session.stopped:
        return
    client = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
    session.client = client
    client._auto_reconnect = True
    try:
        client.start()
    finally:
        session.stopped = True


def _stop_feishu(session: _FeishuSession) -> None:
    """让 SDK 线程退出，避免停用或换凭证后旧连接还在收事件。"""
    session.stopped = True
    thread_loop = session.loop
    client = session.client
    if thread_loop is not None and client is not None and thread_loop.is_running():

        async def shutdown() -> None:
            client._auto_reconnect = False
            await client._disconnect()
            thread_loop.stop()

        thread_loop.call_soon_threadsafe(lambda: thread_loop.create_task(shutdown()))
    elif thread_loop is not None and not thread_loop.is_running():
        thread_loop.call_soon_threadsafe(thread_loop.stop)


def _token_key(app: AppRecord) -> str:
    """密钥变了就换缓存键，避免继续用旧的 tenant_access_token。"""
    digest = hashlib.sha256(app.secret.encode()).hexdigest()[:12]
    return f"{app.id}:{digest}"


def _event_from_receive(data: object) -> InboundEvent | None:
    """从 SDK 对象取出和字典解析器相同的字段。"""
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(getattr(event, "sender", None), "sender_id", None)
    header = getattr(data, "header", None)
    return parse_feishu_event(
        {
            "header": {"event_id": getattr(header, "event_id", "")},
            "event": {
                "message": {
                    "message_id": getattr(message, "message_id", ""),
                    "chat_type": getattr(message, "chat_type", ""),
                    "message_type": getattr(message, "message_type", ""),
                    "content": getattr(message, "content", ""),
                },
                "sender": {"sender_id": {"open_id": getattr(sender, "open_id", "")}},
            },
        }
    )


def _event_from_card(data: object) -> InboundEvent | None:
    """从卡片回调对象取出按钮上的确认码。"""
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    operator = getattr(event, "operator", None)
    header = getattr(data, "header", None)
    value = getattr(action, "value", None)
    return parse_feishu_card(
        {
            "header": {"event_id": getattr(header, "event_id", "")},
            "event": {"operator": {"open_id": getattr(operator, "open_id", "")}, "action": {"value": value or {}}},
        }
    )


def _mapping(value: object) -> dict[str, Any]:
    """只接受字典，避免可选 JSON 字段把类型留成 None。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


async def _forward(dispatch: Dispatch, event: InboundEvent) -> None:
    """把线程里的回调变成主循环上的协程。"""
    await dispatch(event)


def _button(label: str, kind: str, value: dict[str, str]) -> dict[str, Any]:
    """一个回传 value 的按钮。回调里再还原成「同意 短码」。"""
    return {
        "tag": "button",
        "type": kind,
        "text": {"tag": "plain_text", "content": label},
        "value": value,
    }


def _text_content(raw: str) -> str:
    """文本消息的 content 是 JSON 字符串。"""
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(loaded, dict):
        return str(loaded.get("text") or "")
    return ""


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """响应不是 JSON 时当成空对象，交给状态码分支。"""
    try:
        loaded = response.json()
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _feishu_reason(response: httpx.Response) -> str:
    """把 HTTP 错误收成配置类原因。"""
    body = _json_object(response)
    return _reason_from_code(body.get("code"), str(body.get("msg") or ""))


def _reason_from_code(code: object, message: str) -> str:
    """可用范围和接收人错误对用户说明，而不是笼统的发送失败。"""
    numeric = code if isinstance(code, int) else None
    folded = message.casefold()
    if numeric in _CONFIG_CODES or "availability" in folded or "可用范围" in message:
        return "not_in_scope"
    return "send_rejected"
