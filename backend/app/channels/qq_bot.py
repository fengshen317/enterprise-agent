"""官方 QQ 机器人 OpenAPI。只处理 C2C，不使用非官方协议，也不碰 QQ 邮箱连接器。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

import httpx

from app.channels.inbound import InboundEvent
from app.channels.records import AppRecord
from app.channels.send_error import ChannelSendError

logger = logging.getLogger(__name__)
_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_GROUP_EVENTS = {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
# 官方文档：GROUP_AND_C2C_EVENT = 1 << 25。
_INTENT = 1 << 25
Dispatch = Callable[[InboundEvent], Awaitable[None]]
Connect = Callable[..., Any]


def api_base(sandbox: bool) -> str:
    """正式环境与沙箱的 OpenAPI 主机。

    Args:
        sandbox: 是否使用沙箱。

    Returns:
        不带结尾斜杠的主机。
    """
    if sandbox:
        return "https://sandbox.api.sgroup.qq.com"
    return "https://api.sgroup.qq.com"


def parse_qq_dispatch(payload: dict[str, Any]) -> InboundEvent | None:
    """解析网关 Dispatch。群事件标记为群，供上层只记账。

    Args:
        payload: WebSocket 帧。

    Returns:
        入站事件；不是消息事件时为 None。
    """
    name = str(payload.get("t") or "")
    data = _mapping(payload.get("d"))
    if name in _GROUP_EVENTS:
        author = _mapping(data.get("author"))
        return InboundEvent(
            message_id=str(data.get("id") or ""),
            peer_id=str(author.get("member_openid") or author.get("id") or ""),
            text=str(data.get("content") or ""),
            is_group=True,
            kind="text",
        )
    if name != "C2C_MESSAGE_CREATE":
        return None
    author = _mapping(data.get("author"))
    message_type = data.get("message_type")
    message_id = str(data.get("id") or "")
    return InboundEvent(
        message_id=message_id,
        peer_id=str(author.get("user_openid") or author.get("id") or ""),
        text=str(data.get("content") or ""),
        is_group=False,
        kind="text" if message_type in (None, 0) else "other",
        reply_message_id=message_id,
    )


def approval_keyboard(text: str, code: str) -> dict[str, Any]:
    """内嵌键盘。按钮会替用户发出「同意 短码」，仍由原审批服务决定。

    Args:
        text: Markdown 正文。
        code: 四位确认码。

    Returns:
        单聊消息请求体，不含 msg_id。
    """
    return {
        "msg_type": 2,
        "markdown": {"content": text},
        "keyboard": {"content": {"rows": [{"buttons": [_key(f"同意 {code}"), _key(f"拒绝 {code}")]}]}},
    }


class QQBotClient:
    """QQ 机器人的令牌、单聊发送和 WebSocket 网关。"""

    def __init__(self, http: httpx.AsyncClient | None = None, connect: Connect | None = None) -> None:
        """注入 HTTP 与 WebSocket。

        Args:
            http: httpx 客户端。
            connect: websockets.connect 兼容工厂。
        """
        self.http = http or httpx.AsyncClient(timeout=20)
        self._owns_http = http is None
        self._connect = connect
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._next_send: dict[str, float] = {}

    async def aclose(self) -> None:
        """关闭自己创建的 HTTP 客户端。"""
        if self._owns_http:
            await self.http.aclose()

    async def send_text(
        self,
        app: AppRecord,
        openid: str,
        text: str,
        *,
        msg_id: str = "",
        msg_seq: int | None = None,
    ) -> None:
        """发文本。带 msg_id 时是被动回复，不带则是主动消息。

        Args:
            app: QQ 机器人应用。
            openid: 用户 openid。
            text: 正文。
            msg_id: 被动回复对应的用户消息。
            msg_seq: 同一条消息上的回复序号，1 到 4。

        Raises:
            ChannelSendError: 平台拒绝。
        """
        body: dict[str, Any] = {"msg_type": 0, "content": text}
        self._attach_passive(body, msg_id, msg_seq)
        await self._post_message(app, openid, body)

    async def send_typing(self, app: AppRecord, openid: str, msg_id: str, msg_seq: int) -> None:
        """发送「正在输入」。它和文本共用被动回复次数，所以序号必须递增。

        Args:
            app: QQ 机器人应用。
            openid: 用户 openid。
            msg_id: 用户刚发来的消息。
            msg_seq: 本次占用的序号。

        Raises:
            ChannelSendError: 平台拒绝。
        """
        body = {
            "msg_type": 6,
            "input_notify": {"input_type": 1, "input_second": 60},
            "msg_id": msg_id,
            "msg_seq": msg_seq,
        }
        await self._post_message(app, openid, body)

    async def send_keyboard(
        self,
        app: AppRecord,
        openid: str,
        payload: dict[str, Any],
        *,
        msg_id: str = "",
        msg_seq: int | None = None,
    ) -> None:
        """发送带按钮的 Markdown。权限不足时调用方改发纯文本。

        Args:
            app: QQ 机器人应用。
            openid: 用户 openid。
            payload: msg_type=2 的消息体。
            msg_id: 被动回复消息。
            msg_seq: 回复序号。

        Raises:
            ChannelSendError: 平台拒绝。
        """
        body = dict(payload)
        self._attach_passive(body, msg_id, msg_seq)
        await self._post_message(app, openid, body)

    async def serve(self, app: AppRecord, dispatch: Dispatch) -> None:
        """连接官方网关，只把单聊和群事件交给上层。

        Args:
            app: QQ 机器人应用。
            dispatch: 入站处理。
        """
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect
        delay = 1.0
        while True:
            try:
                token = await self._access_token(app)
                url = await self._gateway(app, token)
                async with connect(url) as socket:
                    delay = 1.0
                    await _run_gateway(socket, token, dispatch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("qq_gateway_reconnect", extra={"app_id": str(app.id)})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _post_message(self, app: AppRecord, openid: str, body: dict[str, Any]) -> None:
        """POST /v2/users/{openid}/messages。主动消息失败通常是好友或开关问题。"""
        await self._throttle(openid)
        token = await self._access_token(app)
        url = f"{api_base(app.sandbox)}/v2/users/{openid}/messages"
        try:
            response = await self.http.post(url, headers={"Authorization": f"QQBot {token}"}, json=body)
        except httpx.HTTPError as exc:
            raise ChannelSendError("send_failed", retryable=True) from exc
        if response.status_code == 429:
            raise ChannelSendError("rate_limited", retryable=False)
        if response.status_code >= 500:
            raise ChannelSendError("send_failed", retryable=True)
        if response.status_code >= 400:
            raise ChannelSendError(_qq_reason(response, bool(body.get("msg_id"))), retryable=False)

    async def _access_token(self, app: AppRecord) -> str:
        """用 AppId 和 ClientSecret 换 access_token。"""
        cached = self._tokens.get(str(app.id))
        if cached is not None and cached[1] > monotonic() + 60:
            return cached[0]
        try:
            response = await self.http.post(
                _TOKEN_URL,
                json={"appId": app.external_app_id, "clientSecret": app.secret},
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("token_failed", retryable=True) from exc
        try:
            loaded = response.json()
        except json.JSONDecodeError:
            loaded = {}
        token = str(loaded.get("access_token") or "") if isinstance(loaded, dict) else ""
        if response.status_code >= 400 or not token:
            raise ChannelSendError("token_failed", retryable=False)
        expire = float(loaded.get("expires_in") or 7200) if isinstance(loaded, dict) else 7200
        self._tokens[str(app.id)] = (token, monotonic() + expire)
        return token

    async def _gateway(self, app: AppRecord, token: str) -> str:
        """读取 WebSocket 网关地址。"""
        try:
            response = await self.http.get(
                f"{api_base(app.sandbox)}/gateway",
                headers={"Authorization": f"QQBot {token}"},
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("gateway_unavailable", retryable=True) from exc
        try:
            loaded = response.json()
        except json.JSONDecodeError:
            loaded = {}
        url = str(loaded.get("url") or "") if isinstance(loaded, dict) else ""
        if response.status_code >= 400 or not url.startswith("wss://"):
            raise ChannelSendError("gateway_unavailable", retryable=True)
        return url

    async def _throttle(self, openid: str) -> None:
        """企业认证机器人主动消息约 10 QPS，这里按用户留出间隔。"""
        async with self._lock:
            now = monotonic()
            due = self._next_send.get(openid, 0)
            self._next_send[openid] = max(now, due) + 0.1
            delay = max(0.0, due - now)
        if delay:
            await asyncio.sleep(delay)

    @staticmethod
    def _attach_passive(body: dict[str, Any], msg_id: str, msg_seq: int | None) -> None:
        """被动回复必须同时带消息 id 和递增序号。"""
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq or 1


async def _run_gateway(socket: Any, token: str, dispatch: Dispatch) -> None:
    """完成 Identify 和心跳，并把消息事件抛给上层。"""
    hello = json.loads(await socket.recv())
    interval = 40.0
    if isinstance(hello, dict) and isinstance(hello.get("d"), dict):
        raw_interval = hello["d"].get("heartbeat_interval")
        if isinstance(raw_interval, int | float) and raw_interval > 0:
            interval = float(raw_interval) / 1000
    await socket.send(
        json.dumps(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": _INTENT,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "enterprise-agent", "$device": "enterprise-agent"},
                },
            }
        )
    )
    sequence: dict[str, int | None] = {"s": None}
    heartbeat = asyncio.create_task(_heartbeat(socket, interval, sequence))
    try:
        async for raw in socket:
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                continue
            if isinstance(frame.get("s"), int):
                sequence["s"] = frame["s"]
            op = frame.get("op")
            if op == 7:
                return
            if op == 9:
                raise ChannelSendError("gateway_invalid", retryable=False)
            event = parse_qq_dispatch(frame)
            if event is not None:
                await dispatch(event)
    finally:
        heartbeat.cancel()


async def _heartbeat(socket: Any, interval: float, sequence: dict[str, int | None]) -> None:
    """按 Hello 的间隔发送心跳。"""
    try:
        while True:
            await asyncio.sleep(interval)
            await socket.send(json.dumps({"op": 1, "d": sequence["s"]}))
    except asyncio.CancelledError:
        raise


def _mapping(value: object) -> dict[str, Any]:
    """网关帧里的对象字段。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _key(label: str) -> dict[str, Any]:
    """指令按钮。enter 为真时 QQ 会把 data 当成用户发出的文本。"""
    return {
        "id": label,
        "render_data": {"label": label, "visited_label": label},
        "action": {"type": 2, "permission": {"type": 2}, "data": label, "enter": True},
    }


def _qq_reason(response: httpx.Response, passive: bool) -> str:
    """主动消息失败多半是好友关系或用户关闭了主动消息。"""
    try:
        loaded = response.json()
    except json.JSONDecodeError:
        loaded = {}
    message = str(loaded.get("message") or "") if isinstance(loaded, dict) else ""
    folded = message.casefold()
    if "friend" in folded or "好友" in message:
        return "not_friend"
    if not passive and ("主动" in message or "proactive" in folded or "closed" in folded):
        return "proactive_disabled"
    if passive:
        return "passive_rejected"
    return "send_rejected"
