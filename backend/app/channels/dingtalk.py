"""钉钉企业内部机器人：Stream 收消息，oToMessages/batchSend 主动发单聊。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx

from app.channels.inbound import InboundEvent
from app.channels.records import AppRecord
from app.channels.send_error import ChannelSendError

logger = logging.getLogger(__name__)
TOPIC = "/v1.0/im/bot/messages/get"
_OPEN = "https://api.dingtalk.com/v1.0/gateway/connections/open"
_TOKEN = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_SEND = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
# 这些都是权限、会话或限流名单，重试同一请求不会成功。
_CONFIG_CODES = {
    "robot.oto.notExist": "no_prior_chat",
    "chatbotId.notAllow.sendOTO": "oto_not_allowed",
    "invalidParameter.robotCode.notExsit": "robot_missing",
    "staffId.notExisted": "staff_missing",
}
Dispatch = Callable[[InboundEvent], Awaitable[None]]
Connect = Callable[..., Any]


def parse_dingtalk_message(data: dict[str, Any]) -> InboundEvent:
    """只把 conversationType=1 当成单聊。其他会话仍保留原文摘录供审计。

    Args:
        data: Stream 回调 data 解出的机器人消息。

    Returns:
        入站事件。
    """
    msgtype = str(data.get("msgtype") or "")
    text = ""
    if msgtype == "text" and isinstance(data.get("text"), dict):
        text = str(data["text"].get("content") or "").strip()
    return InboundEvent(
        message_id=str(data.get("msgId") or ""),
        peer_id=str(data.get("senderStaffId") or ""),
        text=text,
        is_group=str(data.get("conversationType") or "") != "1",
        kind="text" if msgtype == "text" else "other",
    )


def classify_dingtalk_send(status: int, body: dict[str, Any]) -> ChannelSendError:
    """把批量单聊的失败分成配置问题和可重试的传输问题。

    限流员工名单、没有单聊会话、不允许主动发单聊都不重试。

    Args:
        status: HTTP 状态。
        body: 响应 JSON。

    Returns:
        发送错误。成功响应不应调用本函数。
    """
    if isinstance(body.get("flowControlledStaffIdList"), list) and body["flowControlledStaffIdList"]:
        return ChannelSendError("flow_control", retryable=False)
    code = str(body.get("code") or "")
    if code in _CONFIG_CODES:
        return ChannelSendError(_CONFIG_CODES[code], retryable=False)
    if status == 429 or code in {"send.too.fast", "send.byToken.tooFast"}:
        return ChannelSendError("rate_limited", retryable=False)
    if status >= 500:
        return ChannelSendError("send_failed", retryable=True)
    return ChannelSendError("send_rejected", retryable=False)


class DingTalkClient:
    """企业内部机器人的 HTTP 与 Stream 客户端。"""

    def __init__(self, http: httpx.AsyncClient | None = None, connect: Connect | None = None) -> None:
        """注入 HTTP 和 WebSocket，便于不连公网测试。

        Args:
            http: httpx 客户端。
            connect: `websockets.connect` 兼容的工厂。
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

    async def send_text(self, app: AppRecord, user_id: str, text: str) -> None:
        """用 robotCode 主动发单聊，不使用会过期的 sessionWebhook。

        Args:
            app: 企业应用。robot_code 为空时用 AppKey。
            user_id: 员工 userId。
            text: 正文。

        Raises:
            ChannelSendError: 平台拒绝。
        """
        await self._throttle(user_id)
        token = await self._access_token(app)
        robot = app.robot_code or app.external_app_id
        try:
            response = await self.http.post(
                _SEND,
                headers={"x-acs-dingtalk-access-token": token},
                json={
                    "robotCode": robot,
                    "userIds": [user_id],
                    "msgKey": "sampleText",
                    "msgParam": json.dumps({"content": text}, ensure_ascii=False),
                },
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("send_failed", retryable=True) from exc
        body = _json_object(response)
        if response.status_code >= 400 or _has_flow_control(body):
            raise classify_dingtalk_send(response.status_code, body)
        if isinstance(body.get("invalidStaffIdList"), list) and user_id in body["invalidStaffIdList"]:
            raise ChannelSendError("staff_missing", retryable=False)

    async def serve(self, app: AppRecord, dispatch: Dispatch) -> None:
        """订阅 /v1.0/im/bot/messages/get。断线后退避重连，直到任务被取消。

        Args:
            app: 企业应用的 Client ID / Secret。
            dispatch: 归一化后的入站处理。
        """
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect
        delay = 1.0
        while True:
            try:
                opened = await self._open_connection(app)
                endpoint = str(opened.get("endpoint") or "")
                ticket = str(opened.get("ticket") or "")
                if not endpoint or not ticket:
                    raise ChannelSendError("stream_unavailable", retryable=True)
                uri = f"{endpoint}?ticket={quote(ticket)}"
                async with connect(uri) as socket:
                    delay = 1.0
                    async for raw in socket:
                        await _handle_frame(socket, raw, dispatch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("dingtalk_stream_reconnect", extra={"app_id": str(app.id)})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _open_connection(self, app: AppRecord) -> dict[str, Any]:
        """向钉钉注册回调订阅并拿到 WebSocket 地址。"""
        try:
            response = await self.http.post(
                _OPEN,
                json={
                    "clientId": app.external_app_id,
                    "clientSecret": app.secret,
                    "subscriptions": [{"type": "CALLBACK", "topic": TOPIC}],
                    "ua": "enterprise-agent",
                    "localIp": "127.0.0.1",
                },
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("stream_unavailable", retryable=True) from exc
        if response.status_code >= 400:
            raise ChannelSendError("stream_unavailable", retryable=True)
        body = _json_object(response)
        return body

    async def _access_token(self, app: AppRecord) -> str:
        """企业内部应用 accessToken。"""
        cached = self._tokens.get(str(app.id))
        if cached is not None and cached[1] > monotonic() + 60:
            return cached[0]
        try:
            response = await self.http.post(
                _TOKEN,
                json={"appKey": app.external_app_id, "appSecret": app.secret},
            )
        except httpx.HTTPError as exc:
            raise ChannelSendError("token_failed", retryable=True) from exc
        body = _json_object(response)
        token = str(body.get("accessToken") or "")
        if response.status_code >= 400 or not token:
            raise ChannelSendError("token_failed", retryable=False)
        self._tokens[str(app.id)] = (token, monotonic() + float(body.get("expireIn") or 7200))
        return token

    async def _throttle(self, user_id: str) -> None:
        """标准版应用 QPS 是 20，单用户发送至少间隔 50 毫秒。"""
        async with self._lock:
            now = monotonic()
            due = self._next_send.get(user_id, 0)
            self._next_send[user_id] = max(now, due) + 0.05
            delay = max(0.0, due - now)
        if delay:
            await asyncio.sleep(delay)


async def _handle_frame(socket: Any, raw: str | bytes, dispatch: Dispatch) -> None:
    """先回 ACK，再把消息交给问答。群消息也交给上层记账。"""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return
    headers = _mapping(message.get("headers"))
    kind = str(message.get("type") or "")
    if kind == "SYSTEM" and headers.get("topic") == "disconnect":
        await socket.close()
        return
    if kind != "CALLBACK":
        return
    await socket.send(json.dumps(_ack(headers)))
    if headers.get("topic") != TOPIC:
        return
    data: Any = message.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return
    if isinstance(data, dict):
        await dispatch(parse_dingtalk_message(data))


def _mapping(value: object) -> dict[str, Any]:
    """Stream 帧里的对象字段。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _ack(headers: dict[str, Any]) -> dict[str, Any]:
    """回传 messageId，钉钉才认为这条回调已经处理。"""
    echoed = {key: headers[key] for key in ("messageId", "topic", "contentType") if key in headers}
    return {"code": 200, "headers": echoed, "message": "OK", "data": "{}"}


def _has_flow_control(body: dict[str, Any]) -> bool:
    """HTTP 200 也可能在名单里带出被限流的员工。"""
    staff = body.get("flowControlledStaffIdList")
    return isinstance(staff, list) and bool(staff)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """非 JSON 响应当成空对象。"""
    try:
        loaded = response.json()
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
