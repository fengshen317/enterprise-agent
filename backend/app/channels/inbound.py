"""各平台入站消息归一成同一结构，后面的指令和审批不再关心原始报文。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundEvent:
    """一条已经判定过单聊/群聊的入站消息。"""

    message_id: str
    peer_id: str
    text: str
    is_group: bool
    kind: str = "text"
    reply_message_id: str = ""
