"""腾讯 iLink Bot HTTP 客户端。

协议依据 openclaw-weixin 当前主线：默认主机 https://ilinkai.weixin.qq.com，
JSON POST，二维码状态用 GET。channel_version 前三段是插件版本，宿主版本写在后缀。
getUpdates 的 ret/errcode 为 -14 时，官方 monitor 会把该账号暂停一小时。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.channels.records import AccountRecord
from app.core.config import Settings

logger = logging.getLogger(__name__)
_ID_FIELDS = frozenset({"message_id", "msg_id", "svr_id"})
_DEFAULT_BASE = "https://ilinkai.weixin.qq.com"


class ILinkError(Exception):
    """iLink 业务错误。不携带 token。"""

    def __init__(self, message: str, *, ret: int | None = None, errcode: int | None = None) -> None:
        """记录返回码。

        Args:
            message: 可写入日志的简短说明。
            ret: 响应 ret。
            errcode: 响应 errcode。
        """
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode


@dataclass(frozen=True)
class ILinkMessage:
    """一条入站消息里本渠道会用到的字段。"""

    message_id: str
    from_user_id: str
    to_user_id: str
    group_id: str
    context_token: str
    text: str
    message_type: int | None
    message_state: int | None


@dataclass(frozen=True)
class UpdatesPage:
    """一次 getUpdates 的结果。超时视为空页，不算失败。"""

    messages: list[ILinkMessage]
    cursor: str
    session_expired: bool
    long_poll_ms: int | None


@dataclass(frozen=True)
class QrCode:
    """绑定二维码。img_content 在官方客户端里按链接展示。"""

    qrcode: str
    img_content: str


@dataclass(frozen=True)
class QrStatus:
    """二维码轮询结果。超时折算成 wait。"""

    status: str
    bot_token: str = ""
    ilink_bot_id: str = ""
    baseurl: str = ""
    ilink_user_id: str = ""
    redirect_host: str = ""


def encode_client_version(channel_version: str) -> str:
    """把插件版本编成 iLink-App-ClientVersion（0x00MMNNPP 的十进制）。

    Args:
        channel_version: 例如 3.0.0/enterprise-agent-0.1.0。斜杠后的宿主版本不参与编码。

    Returns:
        十进制字符串。无法识别时返回 0。
    """
    head = channel_version.split("/", 1)[0].strip().lstrip("vV")
    parts = head.split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            return "0"
        numbers.append(int(digits) & 0xFF)
    while len(numbers) < 3:
        numbers.append(0)
    major, minor, patch = numbers
    return str((major << 16) | (minor << 8) | patch)


def random_wechat_uin() -> str:
    """生成 X-WECHAT-UIN：随机 uint32 的十进制再做 base64。

    Returns:
        请求头取值。
    """
    return base64.b64encode(str(secrets.randbelow(2**32)).encode()).decode()


def quote_large_ids(raw: str) -> str:
    """把 message_id 等 uint64 在解析前改成字符串，避免超过 2^53 时丢精度。

    Args:
        raw: 原始 JSON 文本。

    Returns:
        标识字段被引号包住的 JSON 文本。
    """
    output: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        if raw[index] != '"':
            output.append(raw[index])
            index += 1
            continue
        string_start = index
        index += 1
        escaped = False
        while index < length:
            char = raw[index]
            index += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                break
        string_token = raw[string_start:index]
        output.append(string_token)
        cursor = index
        while cursor < length and raw[cursor].isspace():
            cursor += 1
        if cursor >= length or raw[cursor] != ":":
            continue
        try:
            key = json.loads(string_token)
        except json.JSONDecodeError:
            continue
        if not isinstance(key, str) or key not in _ID_FIELDS:
            continue
        output.append(raw[index : cursor + 1])
        cursor += 1
        while cursor < length and raw[cursor].isspace():
            output.append(raw[cursor])
            cursor += 1
        number_start = cursor
        if cursor < length and raw[cursor] == "-":
            cursor += 1
        while cursor < length and raw[cursor].isdigit():
            cursor += 1
        if cursor > number_start and not (cursor == number_start + 1 and raw[number_start] == "-"):
            output.append(f'"{raw[number_start:cursor]}"')
            index = cursor
        else:
            index = number_start
    return "".join(output)


def safe_https_base(value: str, fallback: str) -> str:
    """只接受 https，并拒绝回环、内网和保留地址，避免绑定响应把请求打进内网。

    Args:
        value: 服务端返回的 baseurl 或 redirect_host。
        fallback: 不合法时使用的默认地址。

    Returns:
        规范化后的 https 源。
    """
    chosen = _https_origin(value) or _https_origin(fallback) or _DEFAULT_BASE
    return chosen


def _https_origin(value: str) -> str | None:
    """解析单个 https 源。

    Args:
        value: 候选地址。

    Returns:
        合法源；不合法时返回 None。
    """
    parsed = urlsplit((value or "").strip())
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or _blocked_host(host):
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}"


def _blocked_host(host: str) -> bool:
    """判断主机是否不该作为 iLink API 地址。

    Args:
        host: 主机名或 IP。

    Returns:
        是否拒绝。
    """
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost"} or lowered.endswith(".local") or lowered.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _as_dict(value: object) -> dict[str, object]:
    """把 JSON 对象收成字符串键的字典。

    Args:
        value: 任意 JSON 值。

    Returns:
        字典；其他类型返回空字典。
    """
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _as_str(value: object) -> str:
    """读取字符串字段。

    Args:
        value: JSON 值。

    Returns:
        字符串，或空串。
    """
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    """读取整数返回码。bool 不当成整数。

    Args:
        value: JSON 值。

    Returns:
        整数或 None。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _message_text(item_list: object) -> str:
    """提取文字和语音转写。图片等非文本不拼进正文。

    Args:
        item_list: 消息 item_list。

    Returns:
        拼接后的文本。
    """
    if not isinstance(item_list, list):
        return ""
    parts: list[str] = []
    for item in item_list:
        body = _as_dict(item)
        kind = _as_int(body.get("type"))
        if kind == 1:
            text = _as_str(_as_dict(body.get("text_item")).get("text"))
        elif kind == 3:
            text = _as_str(_as_dict(body.get("voice_item")).get("text"))
        else:
            text = ""
        if text:
            parts.append(text)
    return "\n".join(parts)


def _parse_message(value: object) -> ILinkMessage:
    """从一条 WeixinMessage 取出渠道字段。

    Args:
        value: 消息对象。

    Returns:
        规范化消息。
    """
    body = _as_dict(value)
    message_id = body.get("message_id")
    return ILinkMessage(
        message_id=_as_str(message_id) if isinstance(message_id, str) else str(message_id or ""),
        from_user_id=_as_str(body.get("from_user_id")),
        to_user_id=_as_str(body.get("to_user_id")),
        group_id=_as_str(body.get("group_id")),
        context_token=_as_str(body.get("context_token")),
        text=_message_text(body.get("item_list")),
        message_type=_as_int(body.get("message_type")),
        message_state=_as_int(body.get("message_state")),
    )


class ILinkClient:
    """对一个进程内的 iLink HTTP 调用。长轮询和发送共用连接池。"""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        """创建客户端。

        Args:
            settings: 版本号、默认主机和超时。
            http: 测试可注入的 httpx 客户端。
        """
        self.settings = settings
        self._http = http or httpx.AsyncClient()
        self._owned = http is None

    async def aclose(self) -> None:
        """关闭自有连接池。注入的客户端由调用方关闭。"""
        if self._owned:
            await self._http.aclose()

    def base_info(self) -> dict[str, str]:
        """构造每个已鉴权 POST 都要带的 base_info。

        Returns:
            channel_version 与 bot_agent。
        """
        return {
            "channel_version": self.settings.ilink_channel_version,
            "bot_agent": self.settings.ilink_bot_agent,
        }

    async def get_bot_qrcode(self, baseurl: str) -> QrCode:
        """本人绑定：POST get_bot_qrcode，不带 token 和 base_info。

        Args:
            baseurl: 固定从默认 iLink 主机开始。

        Returns:
            二维码值和展示内容。

        Raises:
            ILinkError: 响应缺少 qrcode。
        """
        payload = await self._post(
            baseurl,
            "ilink/bot/get_bot_qrcode",
            {"local_token_list": []},
            token=None,
            timeout_seconds=15,
            query={"bot_type": "3"},
            with_base_info=False,
        )
        qrcode = _as_str(payload.get("qrcode"))
        image = _as_str(payload.get("qrcode_img_content"))
        if not qrcode:
            raise ILinkError("get_bot_qrcode 没有返回二维码")
        return QrCode(qrcode, image)

    async def get_qrcode_status(self, qrcode: str, baseurl: str, verify_code: str | None = None) -> QrStatus:
        """短超时轮询二维码。超时按官方客户端记为 wait。

        Args:
            qrcode: get_bot_qrcode 返回的 qrcode，不是图片内容。
            baseurl: 当前轮询主机，重定向后可能变化。
            verify_code: 手机上的数字，仅在上一次状态要求时附带。

        Returns:
            状态和确认后的凭证。
        """
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            response = await self._http.get(
                f"{safe_https_base(baseurl, self.settings.ilink_base_url)}/ilink/bot/get_qrcode_status",
                params=params,
                headers=self._app_headers(),
                timeout=httpx.Timeout(4.0, connect=10.0),
            )
            response.raise_for_status()
            payload = _as_dict(json.loads(response.text))
        except httpx.TimeoutException:
            return QrStatus("wait")
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.warning("ilink_qr_status_failed", extra={"error_type": type(exc).__name__})
            return QrStatus("wait")
        return QrStatus(
            status=_as_str(payload.get("status")) or "wait",
            bot_token=_as_str(payload.get("bot_token")),
            ilink_bot_id=_as_str(payload.get("ilink_bot_id")),
            baseurl=_as_str(payload.get("baseurl")),
            ilink_user_id=_as_str(payload.get("ilink_user_id")),
            redirect_host=_as_str(payload.get("redirect_host")),
        )

    async def get_updates(self, account: AccountRecord, timeout_seconds: float | None = None) -> UpdatesPage:
        """长轮询 getUpdates。客户端超时返回空页，让调用方用原游标重试。

        Args:
            account: 已绑定账号。
            timeout_seconds: 本次长轮询秒数，默认配置值。

        Returns:
            消息、新游标和是否应暂停。

        Raises:
            ILinkError: 除 -14 和超时以外的失败。
        """
        timeout = timeout_seconds or self.settings.ilink_long_poll_seconds
        cursor = account.updates_cursor or ""
        try:
            payload = await self._post(
                account.baseurl,
                "ilink/bot/getupdates",
                {"get_updates_buf": cursor},
                token=account.token,
                timeout_seconds=timeout + 5,
            )
        except httpx.TimeoutException:
            return UpdatesPage([], cursor, False, None)
        ret = _as_int(payload.get("ret"))
        errcode = _as_int(payload.get("errcode"))
        if ret == -14 or errcode == -14:
            logger.warning(
                "ilink_session_expired",
                extra={"account_id": str(account.id), "ret": ret, "errcode": errcode},
            )
            return UpdatesPage([], cursor, True, _as_int(payload.get("longpolling_timeout_ms")))
        if ret not in (None, 0) or errcode not in (None, 0):
            raise ILinkError(
                f"getUpdates ret={ret} errcode={errcode}",
                ret=ret,
                errcode=errcode,
            )
        raw_messages = payload.get("msgs")
        messages = [_parse_message(item) for item in raw_messages] if isinstance(raw_messages, list) else []
        next_cursor = _as_str(payload.get("get_updates_buf")) or cursor
        return UpdatesPage(messages, next_cursor, False, _as_int(payload.get("longpolling_timeout_ms")))

    async def send_text(self, account: AccountRecord, to_user_id: str, context_token: str, text: str) -> None:
        """发送一条已完成的文本消息，并回传入站 context_token。

        Args:
            account: 发送方账号。
            to_user_id: 接收用户。
            context_token: 入站消息带回的上下文。空串仍发送，服务端是否接受未在真实环境验证。
            text: 一段已切分的文本。

        Raises:
            ILinkError: ret 非 0。
        """
        message: dict[str, object] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        payload = await self._post(
            account.baseurl,
            "ilink/bot/sendmessage",
            {"msg": message},
            token=account.token,
            timeout_seconds=15,
        )
        ret = _as_int(payload.get("ret"))
        if ret not in (None, 0):
            raise ILinkError(f"sendMessage ret={ret}", ret=ret, errcode=_as_int(payload.get("errcode")))

    async def send_typing(self, account: AccountRecord, to_user_id: str, context_token: str, active: bool) -> None:
        """先取 typing_ticket 再发送输入状态。失败只记日志，不挡住回复。

        Args:
            account: 发送方账号。
            to_user_id: 接收用户。
            context_token: 当前会话令牌。
            active: True 为正在输入，False 为取消。
        """
        if not context_token:
            return
        try:
            config = await self._post(
                account.baseurl,
                "ilink/bot/getconfig",
                {"ilink_user_id": to_user_id, "context_token": context_token},
                token=account.token,
                timeout_seconds=10,
            )
            if _as_int(config.get("ret")) not in (None, 0):
                return
            ticket = _as_str(config.get("typing_ticket"))
            if not ticket:
                return
            await self._post(
                account.baseurl,
                "ilink/bot/sendtyping",
                {"ilink_user_id": to_user_id, "typing_ticket": ticket, "status": 1 if active else 2},
                token=account.token,
                timeout_seconds=10,
            )
        except (ILinkError, httpx.HTTPError, json.JSONDecodeError):
            logger.warning("ilink_typing_failed", extra={"account_id": str(account.id)})

    async def notify_start(self, account: AccountRecord) -> None:
        """告诉 iLink 这个账号的长轮询开始了。

        Args:
            account: 已绑定账号。

        Raises:
            ILinkError: 业务返回码非 0。
        """
        await self._notify(account, "ilink/bot/msg/notifystart")

    async def notify_stop(self, account: AccountRecord) -> None:
        """告诉 iLink 这个账号的长轮询停止了。

        Args:
            account: 已绑定账号。

        Raises:
            ILinkError: 业务返回码非 0。
        """
        await self._notify(account, "ilink/bot/msg/notifystop")

    async def _notify(self, account: AccountRecord, path: str) -> None:
        """发送生命周期通知。

        Args:
            account: 已绑定账号。
            path: notifystart 或 notifystop。

        Raises:
            ILinkError: 业务返回码非 0。
        """
        payload = await self._post(account.baseurl, path, {}, token=account.token, timeout_seconds=10)
        ret = _as_int(payload.get("ret"))
        if ret not in (None, 0):
            raise ILinkError(f"{path} ret={ret}", ret=ret, errcode=_as_int(payload.get("errcode")))

    async def _post(
        self,
        baseurl: str,
        path: str,
        body: dict[str, object],
        *,
        token: str | None,
        timeout_seconds: float,
        query: dict[str, str] | None = None,
        with_base_info: bool = True,
    ) -> dict[str, object]:
        """发送 JSON POST 并解析业务 JSON。

        Args:
            baseurl: API 源。
            path: 相对路径。
            body: 请求体。base_info 由本方法补上。
            token: bot_token。二维码请求不传。
            timeout_seconds: 客户端超时秒数。
            query: 查询参数。
            with_base_info: 取码请求按协议不带 base_info。

        Returns:
            响应对象。

        Raises:
            ILinkError: HTTP 或 JSON 失败。
            httpx.TimeoutException: 调用方决定是否当成空轮询。
        """
        payload = dict(body)
        if with_base_info:
            payload["base_info"] = self.base_info()
        try:
            response = await self._http.post(
                f"{safe_https_base(baseurl, self.settings.ilink_base_url)}/{path}",
                params=query,
                headers=self._post_headers(token),
                content=json.dumps(payload, ensure_ascii=False),
                timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            )
            response.raise_for_status()
            return _as_dict(json.loads(quote_large_ids(response.text or "{}")))
        except httpx.TimeoutException:
            raise
        except httpx.HTTPError as exc:
            raise ILinkError(f"{path} http {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise ILinkError(f"{path} 返回的不是 JSON") from exc

    def _app_headers(self) -> dict[str, str]:
        """二维码状态轮询只带应用头，不带 Authorization。

        Returns:
            iLink 应用头。
        """
        return {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": encode_client_version(self.settings.ilink_channel_version),
        }

    def _post_headers(self, token: str | None) -> dict[str, str]:
        """JSON POST 的公共头。有 token 时加 Bearer。

        Args:
            token: bot_token。

        Returns:
            请求头。
        """
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_wechat_uin(),
            **self._app_headers(),
        }
        if token and token.strip():
            headers["Authorization"] = f"Bearer {token.strip()}"
        return headers
