"""出站失败的分类。配置和权限问题不能当成暂时故障去重试。"""

from __future__ import annotations


class ChannelSendError(Exception):
    """平台拒绝发送。retryable 为假时调用方只记账，不再次请求。"""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        """记录可展示的跳过原因。

        Args:
            reason: 写入 channel_messages.skip_reason 的短码。
            retryable: 是否属于短暂故障。当前发送路径只发一次，这个标记供调用方决定文案。
        """
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
