"""定时任务选择要把结果推到哪些渠道。空列表表示不投递，不是失败。"""

from __future__ import annotations

PUSH_CHANNEL_TYPES = ("ilink", "feishu", "dingtalk", "qq")


def normalize_push_channels(push_to_wechat: bool, push_channels: list[str] | None) -> tuple[bool, list[str]]:
    """把旧的微信开关和新的渠道列表收成同一对值。

    没有渠道列表时，仍按微信开关生成。有列表时，以列表为准，并让微信开关表示其中有没有微信。

    Args:
        push_to_wechat: 历史布尔开关。
        push_channels: 显式渠道；None 表示调用方没传。

    Returns:
        同步后的微信开关和渠道列表。
    """
    if push_channels is None:
        return push_to_wechat, ["ilink"] if push_to_wechat else []
    cleaned: list[str] = []
    for item in push_channels:
        if item in PUSH_CHANNEL_TYPES and item not in cleaned:
            cleaned.append(item)
    return "ilink" in cleaned, cleaned


def apply_wechat_flag(channels: list[str], enabled: bool) -> list[str]:
    """只改微信这一项，保留用户已经勾选的其他渠道。

    Args:
        channels: 当前渠道。
        enabled: 是否推送到微信。

    Returns:
        新的渠道列表。
    """
    current = [item for item in channels if item in PUSH_CHANNEL_TYPES and item != "ilink"]
    if enabled:
        return [*current, "ilink"]
    return current


def stored_push_channels(raw: object, push_to_wechat: bool) -> list[str]:
    """读取数据库里的渠道列表。旧数据只有布尔值时按微信开关理解。

    Args:
        raw: JSON 列。
        push_to_wechat: 历史开关。

    Returns:
        渠道列表。
    """
    if isinstance(raw, list):
        values = [item for item in raw if isinstance(item, str) and item in PUSH_CHANNEL_TYPES]
        if values or not push_to_wechat:
            return values
    return ["ilink"] if push_to_wechat else []
