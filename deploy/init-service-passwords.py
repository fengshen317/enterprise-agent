"""初始化 Compose 服务密码，并保留数据卷对应的凭据。"""

import os
import secrets
import sys
from fcntl import LOCK_EX, flock
from pathlib import Path
from tempfile import NamedTemporaryFile

PASSWORD_BYTES = 32
API_UID = 10001
# Elasticsearch 的 _FILE 入口要求非 root 运行账号可读，仅接受 0400 或 0600。
ELASTICSEARCH_UID = 1000
PASSWORD_MODE = 0o400


def ensure_password(directory: Path, variable: str, *, owner: int, data_markers: tuple[Path, ...]) -> None:
    """优先采用部署者提供的密码，其次复用本次部署已保存的值。

    Args:
        directory: 对应的持久化凭据卷。
        variable: 显式密码的环境变量名。
        owner: 需要读取该文件的非 root 容器用户。
        data_markers: 已有数据的标记，防止为旧库生成不匹配的密码。
    """
    configured = os.environ.get(variable, "").encode()
    password_file = directory / "password"
    with (directory / ".lock").open("a") as lock:
        flock(lock, LOCK_EX)
        old = password_file.read_bytes() if password_file.exists() else None
        if not configured and old == b"":
            raise ValueError(f"{variable} 的持久化密码文件为空，请从备份恢复或填写原密码")
        if not configured and old is None and any(marker.exists() for marker in data_markers):
            raise ValueError(f"检测到已有服务数据。请通过 {variable} 提供原密码，或还原对应的凭据卷")

        password = configured or old or secrets.token_hex(PASSWORD_BYTES).encode()
        if password == old:
            password_file.chmod(PASSWORD_MODE)
            os.chown(password_file, owner, 0)
            return
        with NamedTemporaryFile(dir=directory, delete=False) as target:
            temporary = Path(target.name)
            try:
                target.write(password)
                target.flush()
                os.fchown(target.fileno(), owner, 0)
                os.fchmod(target.fileno(), PASSWORD_MODE)
                os.fsync(target.fileno())
                target.close()
                temporary.replace(password_file)
            finally:
                temporary.unlink(missing_ok=True)


def main() -> int:
    """在数据库启动前生成并交付两个独立的密码文件。"""
    os.umask(0o077)
    try:
        ensure_password(
            Path("/run/ea-postgres"),
            "EA_DATABASE_PASSWORD",
            owner=API_UID,
            data_markers=(Path("/var/lib/ea-existing/postgres/PG_VERSION"),),
        )
        search_data = Path("/var/lib/ea-existing/elasticsearch")
        ensure_password(
            Path("/run/ea-ragflow"),
            "EA_RAGFLOW_SERVICE_PASSWORD",
            owner=ELASTICSEARCH_UID,
            data_markers=(
                Path("/var/lib/ea-existing/mysql/mysql"),
                search_data / "nodes",
                search_data / "_state",
                search_data / "indices",
            ),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("无法初始化服务密码，请检查 Compose 凭据卷和数据卷权限。", file=sys.stderr)
        return 1
    print("PostgreSQL 和 RAGFlow 的服务密码已就绪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
