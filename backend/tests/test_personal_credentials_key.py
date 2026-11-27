"""验证首次绑定自动持久化密钥，以及丢失密钥时拒绝覆盖旧凭据。"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.services.personal_credentials import _cipher


async def test_generated_key_survives_restart_and_concurrent_creation(tmp_path: Path) -> None:
    """两个首次绑定进程读取同一落盘密钥，重启后仍能解开原凭据。"""
    import asyncio

    settings = Settings(_env_file=None, connector_credential_key="", connector_credential_key_file=tmp_path / "key")
    session = AsyncMock()
    session.scalar.return_value = False
    first, second = await asyncio.gather(
        _cipher(session, settings, create=True), _cipher(session, settings, create=True)
    )
    ciphertext = first.encrypt(b"private-token")
    assert second.decrypt(ciphertext) == b"private-token"
    restarted = await _cipher(session, settings.model_copy(), create=False)
    assert restarted.decrypt(ciphertext) == b"private-token"


async def test_missing_key_never_replaces_existing_encrypted_bindings(tmp_path: Path) -> None:
    """数据已存在时要求恢复原密钥，防止静默产生无法解密的新旧混合数据。"""
    path = tmp_path / "missing.key"
    settings = Settings(_env_file=None, connector_credential_key="", connector_credential_key_file=path)
    session = AsyncMock()
    session.scalar.return_value = True
    with pytest.raises(ApplicationError, match="恢复原加密密钥"):
        await _cipher(session, settings, create=True)
    assert not path.exists()


async def test_explicit_key_preserves_legacy_credentials(tmp_path: Path) -> None:
    """已有环境密钥优先，不生成另一个文件改变旧凭据的解密方式。"""
    key = Fernet.generate_key()
    settings = Settings(_env_file=None, connector_credential_key=key.decode(),
                        connector_credential_key_file=tmp_path / "unused.key")
    cipher = await _cipher(AsyncMock(), settings, create=True)
    assert cipher.decrypt(Fernet(key).encrypt(b"old-token")) == b"old-token"
    assert not settings.connector_credential_key_file.exists()
