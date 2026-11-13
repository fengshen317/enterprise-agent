"""解析微信一对一会话里的文本指令。"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Literal

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_APPROVE = re.compile(r"^同意\s+([A-Za-z0-9]{4})$")
_REJECT = re.compile(r"^拒绝\s+([A-Za-z0-9]{4})(?:\s+(.{1,200}))?$")
_CODE = re.compile(r"^[A-HJ-NP-Z2-9]{4}$")
CommandKind = Literal["chat", "new", "workspace", "agent", "approve", "reject"]


@dataclass(frozen=True)
class InboundCommand:
    """一条入站文本对应的动作。无法识别时按普通对话处理。"""

    kind: CommandKind
    argument: str = ""
    code: str = ""
    comment: str = ""


def new_approval_code() -> str:
    """生成不含易混字符的四位确认码。

    Returns:
        大写确认码。
    """
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))


def parse_inbound(text: str) -> InboundCommand:
    """识别 /new、/ws、/agent 和审批回复。

    Args:
        text: 用户原文。

    Returns:
        指令或普通对话。
    """
    stripped = text.strip()
    if stripped == "/new":
        return InboundCommand("new")
    if stripped.startswith("/ws ") or stripped.startswith("/ws\t"):
        return InboundCommand("workspace", argument=stripped[3:].strip())
    if stripped == "/agent":
        return InboundCommand("agent")
    if stripped.startswith("/agent ") or stripped.startswith("/agent\t"):
        return InboundCommand("agent", argument=stripped[6:].strip())
    approved = _APPROVE.fullmatch(stripped)
    if approved is not None:
        code = approved.group(1).upper()
        if _CODE.fullmatch(code):
            return InboundCommand("approve", code=code)
    rejected = _REJECT.fullmatch(stripped)
    if rejected is not None:
        code = rejected.group(1).upper()
        if _CODE.fullmatch(code):
            comment = (rejected.group(2) or "").strip()
            return InboundCommand("reject", code=code, comment=comment)
    return InboundCommand("chat")


def chunk_text(text: str, size: int) -> list[str]:
    """按字符数切分，避免单条文本超过 iLink 常见的长度限制。

    Args:
        text: 待发送正文。
        size: 每段最大字符数。

    Returns:
        非空分段。空文本返回空列表。
    """
    if not text:
        return []
    step = max(1, size)
    return [text[index : index + step] for index in range(0, len(text), step)]
