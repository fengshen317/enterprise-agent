"""管理用于加密持久凭证的本地密钥文件。"""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet


def cipher_for(path: Path, *, create: bool) -> Fernet:
    """读取部署密钥，首次写入时原子创建并复用同一文件。"""
    try:
        return Fernet(path.read_bytes().strip())
    except FileNotFoundError:
        if not create:
            raise
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(Fernet.generate_key())
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
    return Fernet(path.read_bytes().strip())
