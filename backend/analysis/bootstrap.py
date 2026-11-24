"""容器内的可信引导程序负责写入输入、降权和执行超时控制。"""

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

_INPUT_DIR = Path("/inputs")
_PROGRAM = _INPUT_DIR / ".program.py"
_EXECUTE = Path("/opt/report-sandbox/execute.py")
_INPUT_NAME = re.compile(r"input_[1-9][0-9]{0,2}\.(?:csv|xlsx|json)")
_MAX_REQUEST_BYTES = 24 * 1024 * 1024
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_CODE_BYTES = 64 * 1024
_NONROOT_ID = 65532
_SCRIPT_TIMEOUT_EXIT = 124
_BOOTSTRAP_FAILURE_EXIT = 125


def _prepare_inputs() -> float:
    """接收一次性的任务数据，并生成根用户持有的只读输入。"""
    header = sys.stdin.buffer.readline(12)
    if not header.endswith(b"\n") or not header[:-1].isdigit():
        raise ValueError("request prefix")
    size = int(header)
    if not 0 < size <= _MAX_REQUEST_BYTES:
        raise ValueError("request size")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        raise ValueError("incomplete request")
    request = json.loads(payload)
    code: bytes = request["code"].encode("utf-8")
    files = request["files"]
    timeout = float(request["timeout_seconds"])
    if not 0 < timeout <= 120 or len(code) > _MAX_CODE_BYTES or len(files) > 16:
        raise ValueError("request limits")
    total_size = 0
    for name, encoded in files.items():
        if not _INPUT_NAME.fullmatch(name):
            raise ValueError("input name")
        content = base64.b64decode(encoded, validate=True)
        total_size += len(content)
        if total_size > _MAX_INPUT_BYTES:
            raise ValueError("input size")
        path = _INPUT_DIR / name
        path.write_bytes(content)
        path.chmod(0o444)
    _PROGRAM.write_bytes(code)
    _PROGRAM.chmod(0o444)
    _INPUT_DIR.chmod(0o555)
    return timeout


def main() -> int:
    """启动低权限进程，在独立的父进程中负责计时。"""
    try:
        timeout = _prepare_inputs()
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError):
        print("EA_REPORT_BOOTSTRAP_ERROR", file=sys.stderr)
        return _BOOTSTRAP_FAILURE_EXIT
    print("EA_REPORT_STARTED", file=sys.stderr)
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", str(_EXECUTE)],
            stdin=subprocess.DEVNULL,
            cwd="/tmp",
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "HOME": "/tmp",
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            },
            # 单独的 UID 让脚本无法修改输入文件或停止负责超时的父进程。
            user=_NONROOT_ID,
            group=_NONROOT_ID,
            extra_groups=[],
            umask=0o077,
        )
        result = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 信任进程没有 CAP_KILL，立即退出容器 PID 1，由内核终止脚本进程。
        return _SCRIPT_TIMEOUT_EXIT
    except OSError:
        return _BOOTSTRAP_FAILURE_EXIT
    # PID 1 退出时由容器运行时销毁子进程，包含脚本另外启动的进程。
    return 1 if result < 0 else result


if __name__ == "__main__":
    sys.exit(main())
