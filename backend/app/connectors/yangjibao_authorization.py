"""养基宝扫码协议与仅对发起人可见的临时会话。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import time
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.connectors.base import ConnectorError
from app.schemas.auth import ResourceScope

_BASE_URL = "https://browser-plug-api.yangjibao.com"
# 与固定版本 fundpeek 的浏览器插件客户端协议一致。此值是公共客户端标识，不是用户凭据。
_SIGNING_SUFFIX = "YxmKSrQR4uoJ5lOoWIhcbd7SlUEh9OOc"
_SCAN_TTL = timedelta(seconds=60)
_QR_HOST_SUFFIXES = ("weixin.qq.com", "wx.qq.com", "yangjibao.com")
ScanState = Literal["awaiting_user", "completed", "expired", "failed"]


async def _request(path: str) -> dict[str, object]:
    """向固定的上游发出扫码请求，只返回有效业务数据。"""
    timestamp = str(int(time()))
    sign = hashlib.md5(f"{path}{timestamp}{_SIGNING_SUFFIX}".encode(), usedforsecurity=False).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(
                _BASE_URL + path,
                headers={"Request-Time": timestamp, "Request-Sign": sign, "User-Agent": "fundpeek/1.0"},
            )
            response.raise_for_status()
            body: object = response.json()
    except (httpx.HTTPError, ValueError):
        raise ConnectorError("network_error", "暂时无法连接养基宝，请稍后重试") from None
    if not isinstance(body, dict) or body.get("code") != 200 or not isinstance(data := body.get("data"), dict):
        raise ConnectorError("invalid_output", "养基宝未返回可识别的扫码状态")
    return cast(dict[str, object], data)


def _valid_qr_url(value: object) -> bool:
    """只展示可识别的养基宝或微信扫码内容。"""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\\]", value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            return False
        # 微信扫一扫使用原始 HTTP /q/ 内容，页面只将其编码为图片。
        if parsed.scheme == "http":
            return host == "weixin.qq.com" and parsed.port is None and parsed.path.startswith("/q/")
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and any(host == suffix or host.endswith("." + suffix) for suffix in _QR_HOST_SUFFIXES)
        )
    except ValueError:
        return False


@dataclass
class YangJiBaoScan:
    """二维码只存在于当前进程，用户凭据由上层保存。"""

    scope: ResourceScope
    qr_id: str = field(repr=False)
    content: str = field(repr=False)
    expires_at: datetime
    state: ScanState = "awaiting_user"

    @classmethod
    async def start(cls, scope: ResourceScope) -> YangJiBaoScan:
        """为当前工作台账号获取一次性的微信扫码内容。"""
        data = await _request("/qr_code")
        qr_id, content = data.get("id"), data.get("url")
        if not isinstance(qr_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", qr_id) or not _valid_qr_url(content):
            raise ConnectorError("invalid_output", "养基宝未返回可用的微信二维码")
        return cls(scope, qr_id, cast(str, content), datetime.now(UTC) + _SCAN_TTL)

    def expire(self) -> None:
        """授权过期时清除可用于扫码的内容。"""
        if self.state == "awaiting_user" and datetime.now(UTC) >= self.expires_at:
            self.finish("expired")

    def finish(self, state: Literal["completed", "expired", "failed"]) -> None:
        """只保留终态，释放临时二维码信息。"""
        self.state = state
        self.qr_id = self.content = ""

    async def poll(self) -> SecretStr | None:
        """检查一次扫码状态，只在上游确认后交出凭据。"""
        self.expire()
        if self.state != "awaiting_user":
            return None
        data = await _request("/qr_code_state/" + self.qr_id)
        self.expire()
        if self.state != "awaiting_user":
            return None
        match str(data.get("state")):
            case "2":
                if isinstance(token := data.get("token"), str) and token.strip():
                    return SecretStr(token)
                self.finish("failed")
            case "3":
                self.finish("expired")
        return None

    def snapshot(self) -> dict[str, object]:
        """返回状态和二维码，省略上游会话标识与用户凭据。"""
        self.expire()
        messages = {
            "awaiting_user": "请打开微信扫一扫，按手机提示确认授权。",
            "completed": "养基宝已连接到当前工作台账号。",
            "expired": "二维码已过期，请重新获取。",
            "failed": "养基宝授权失败，请重新扫码。",
        }
        return {
            "auth_url": "",
            "qr_content": self.content,
            "output": messages[self.state],
            "already_logged_in": False,
            "state": self.state,
            "expires_at": self.expires_at.isoformat(),
        }
