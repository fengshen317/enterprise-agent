"""用 Fernet 加密 iLink bot_token，密钥只来自配置。"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.errors import ApplicationError

logger = logging.getLogger(__name__)


class ChannelCrypto:
    """bot_token 的加解密。未配置密钥时渠道保持关闭。"""

    def __init__(self, settings: Settings) -> None:
        """从配置加载 Fernet 密钥。

        Args:
            settings: 应用配置。空密钥表示不启用渠道。
        """
        raw = settings.channel_token_key.get_secret_value().strip()
        self._fernet: Fernet | None = None
        if not raw:
            return
        try:
            self._fernet = Fernet(raw.encode())
        except (ValueError, TypeError):
            logger.error("channel_token_key_invalid")

    @property
    def configured(self) -> bool:
        """密钥是否可用。"""
        return self._fernet is not None

    def encrypt(self, token: str) -> str:
        """加密 bot_token。

        Args:
            token: iLink 返回的明文凭证。

        Returns:
            可入库的密文。

        Raises:
            ApplicationError: 渠道密钥未配置。
        """
        fernet = self._require()
        return fernet.encrypt(token.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """解密 bot_token。

        Args:
            ciphertext: 库中的密文。

        Returns:
            明文凭证。

        Raises:
            ApplicationError: 密钥未配置或密文无法解密。
        """
        fernet = self._require()
        try:
            return fernet.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            raise ApplicationError("CHANNEL_TOKEN_INVALID", "微信凭证无法解密", 500) from exc

    def _require(self) -> Fernet:
        """返回已加载的 Fernet。

        Returns:
            Fernet 实例。

        Raises:
            ApplicationError: 未配置密钥。
        """
        if self._fernet is None:
            raise ApplicationError("CHANNEL_NOT_CONFIGURED", "微信渠道未配置密钥", 503)
        return self._fernet
