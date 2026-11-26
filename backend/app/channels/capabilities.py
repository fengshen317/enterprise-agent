"""渠道能力标记。入站指令、窗口和主动推送只看这些标记，不复制一套微信流程。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.channels.records import PeerLink
from app.core.config import Settings

CHANNEL_TYPES = ("ilink", "feishu", "dingtalk", "qq")
ENTERPRISE_CHANNEL_TYPES = ("feishu", "dingtalk", "qq")
PUSH_CHANNEL_TYPES = CHANNEL_TYPES


@dataclass(frozen=True)
class ChannelCapabilities:
    """一个渠道对回复窗口、主动推送和消息形态的声明。"""

    channel_type: str
    label: str
    has_reply_window: bool
    window_hours: float
    window_reply_budget: int | None
    supports_proactive_without_prior_chat: bool
    proactive_uses_reply_window: bool
    supports_typing: bool
    supports_action_buttons: bool
    supports_markdown: bool
    max_text_chars: int
    text_progress: bool


def capability_for(channel_type: str) -> ChannelCapabilities:
    """返回渠道能力。未知类型按不能主动推送处理，避免误发。

    Args:
        channel_type: ilink、feishu、dingtalk 或 qq。

    Returns:
        能力标记。
    """
    if channel_type == "feishu":
        return ChannelCapabilities(
            "feishu",
            "飞书",
            False,
            0,
            None,
            True,
            False,
            False,
            True,
            True,
            4000,
            True,
        )
    if channel_type == "dingtalk":
        # 主动单聊要求员工先打开过会话，否则接口返回 robot.oto.notExist。
        return ChannelCapabilities(
            "dingtalk",
            "钉钉",
            False,
            0,
            None,
            False,
            False,
            False,
            False,
            True,
            4000,
            True,
        )
    if channel_type == "qq":
        # 被动回复是 60 分钟、每条消息 4 次；主动消息不占用这个窗口。
        return ChannelCapabilities(
            "qq",
            "QQ",
            True,
            1,
            4,
            True,
            False,
            True,
            True,
            True,
            1500,
            False,
        )
    return ChannelCapabilities(
        "ilink",
        "微信",
        True,
        24,
        None,
        False,
        True,
        True,
        False,
        False,
        1800,
        True,
    )


def window_hours(caps: ChannelCapabilities, settings: Settings) -> float:
    """微信窗口跟部署配置走，其他渠道用平台自己的时长。

    Args:
        caps: 渠道能力。
        settings: 含 EA_CHANNEL_REPLY_WINDOW_HOURS。

    Returns:
        窗口小时数。没有窗口时为 0。
    """
    if not caps.has_reply_window:
        return 0
    if caps.channel_type == "ilink":
        return float(settings.channel_reply_window_hours)
    return caps.window_hours


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


def delivery_skip_reason(
    caps: ChannelCapabilities,
    peer: PeerLink | None,
    now: datetime,
    settings: Settings,
) -> str | None:
    """判断定时结果或审批能不能主动发出去。

    Args:
        caps: 渠道能力。
        peer: 最近一次入站。
        now: 当前时间。
        settings: 微信窗口小时数。

    Returns:
        跳过原因；可以发送时为 None。
    """
    if caps.proactive_uses_reply_window:
        return window_skip_reason(peer, now, int(window_hours(caps, settings)))
    if not caps.supports_proactive_without_prior_chat and (peer is None or peer.last_inbound_at is None):
        return "no_prior_chat"
    return None


def reminder_due(
    caps: ChannelCapabilities,
    peer: PeerLink | None,
    now: datetime,
    settings: Settings,
) -> datetime | None:
    """窗口后段才提醒一次。没有回复窗口的渠道不提醒。

    Args:
        caps: 渠道能力。
        peer: 最近入站。
        now: 当前时间。
        settings: 提醒提前量。微信仍用完整的提前小时数。

    Returns:
        本次入站时间，表示应该提醒；不该提醒时为 None。
    """
    if not caps.has_reply_window or settings.channel_window_reminder_hours <= 0:
        return None
    if peer is None or peer.last_inbound_at is None:
        return None
    hours = window_hours(caps, settings)
    if delivery_skip_reason(caps, peer, now, settings) is not None:
        return None
    inbound = peer.last_inbound_at if peer.last_inbound_at.tzinfo else peer.last_inbound_at.replace(tzinfo=UTC)
    remaining = inbound + timedelta(hours=hours) - now
    threshold = float(settings.channel_window_reminder_hours)
    # 短窗口不能用比窗口还长的提前量，否则一收到消息就会提醒。
    if caps.channel_type != "ilink":
        threshold = min(threshold, hours / 2)
    if remaining <= timedelta(0) or remaining > timedelta(hours=threshold):
        return None
    if peer.reminder_for_inbound_at == inbound:
        return None
    return inbound


def welcome_text(channel_type: str) -> str:
    """绑定后的第一段说明。微信文案保持原句，避免把 24 小时限制抄到没有窗口的渠道。

    Args:
        channel_type: 渠道类型。

    Returns:
        发给用户的说明。
    """
    if channel_type == "ilink":
        return (
            "已绑定微信，直接发消息就能对话。\n"
            "指令：/new 新对话，/ws 空间标识 切换空间，/agent 切换 Agent，/help 再看一遍说明。\n"
            "只有一项待确认的写操作时，可以回复「同意」或「1」，也可以回复「拒绝」或「2」加原因。"
            "有多项时请带上确认码。删除和外发只能在网页里确认。\n"
            "超过 24 小时没有发消息，定时结果不会推到微信。窗口快结束时我会提醒你回复任意内容。"
        )
    shared = (
        f"已绑定{capability_for(channel_type).label}，直接发消息就能对话。\n"
        "指令：/new 新对话，/ws 空间标识 切换空间，/agent 切换 Agent，/help 再看一遍说明。\n"
        "只有一项待确认的写操作时，可以回复「同意」或「1」，也可以回复「拒绝」或「2」加原因。"
        "有多项时请带上确认码。删除和外发只能在网页里确认。"
    )
    if channel_type == "feishu":
        return (
            shared + "\n飞书没有 24 小时回复窗口。你在应用可用范围内时，定时结果会主动发到这个单聊。"
            "同一用户发送大约 5 次/秒。"
        )
    if channel_type == "dingtalk":
        return (
            shared + "\n请先在钉钉里打开与机器人的单聊再收定时结果，否则会提示没有有效的单聊会话。"
            "标准版付费接口大约每月 1 万次、20 QPS；更高套餐以开发者后台资源管理为准。"
        )
    if channel_type == "qq":
        return (
            shared + "\n被动回复只在你发出一条消息后的 60 分钟内有效，每条消息最多回复 4 次，"
            "所以处理过程只用「正在输入」。"
            "定时结果走主动消息：需要先加机器人为好友，且没有在 QQ 里关闭「允许主动发送」。"
        )
    return shared


def reminder_text(channel_type: str) -> str:
    """窗口即将关闭时的提醒。只用于真正有回复窗口的渠道。

    Args:
        channel_type: 渠道类型。

    Returns:
        提醒正文。
    """
    if channel_type == "qq":
        return "60 分钟被动回复窗口快结束了。再发一条消息可以继续在这里收被动回复；定时结果仍尝试走主动消息。"
    return "回复任意内容可继续接收推送"


def bind_instructions(channel_type: str) -> str:
    """未绑定用户能看到的唯一回复，不能据此猜工作台账号。

    Args:
        channel_type: 企业渠道类型。

    Returns:
        绑定说明。
    """
    label = capability_for(channel_type).label
    return (
        f"这个{label}还没有绑定工作台账号，消息不会进入任何对话。"
        f"请打开工作台「通讯通道 → {label}」生成绑定码，然后只在这个单聊里发送：/bind 绑定码。"
    )


def conversation_title(channel_type: str) -> str:
    """渠道固定对话的标题。

    Args:
        channel_type: 渠道类型。

    Returns:
        对话标题。
    """
    return f"{capability_for(channel_type).label}对话"
