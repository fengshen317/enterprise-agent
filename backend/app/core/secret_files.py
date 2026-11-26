"""管理需要跨重启保留的本地敏感配置文件。"""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet


def read_or_create_secret(path: Path, value: bytes | None) -> bytes:
    """读取已有密钥，提供备用值时才原子创建文件。"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if value is None:
            raise
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            try:
                path.hardlink_to(temporary)
            except FileExistsError:
                # 另一进程创建的密钥优先，防止同一部署出现两个密钥。
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return path.read_bytes()


def cipher_for(path: Path, *, create: bool) -> Fernet:
    """读取部署加密密钥，首次写入时复用同一文件。"""
    return Fernet(read_or_create_secret(path, Fernet.generate_key() if create else None).strip())
