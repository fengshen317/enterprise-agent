"""同时启动本地前后端，并统一管理子进程生命周期。"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_DIR / "frontend"
BACKEND_DIR = PROJECT_DIR / "backend"
HOST = "127.0.0.1"
BACKEND_PORT = 8000
FRONTEND_PORT = 8686
POLL_SECONDS = 0.3
STOP_TIMEOUT_SECONDS = 5
INTERRUPTED_EXIT_CODE = 130


def check_port(port: int) -> None:
    """提前检测端口，避免启动重复后端进程。

    Args:
        port: 待绑定的本地端口。
    """
    with socket.socket() as listener:
        listener.bind((HOST, port))


def start_process(command: list[str], directory: Path) -> subprocess.Popen[bytes]:
    """为服务创建独立进程组，便于清理重载器和其子进程。

    Args:
        command: 固定的服务启动命令。
        directory: 服务工作目录。

    Returns:
        当前脚本拥有的服务进程。
    """
    return subprocess.Popen(
        command,
        cwd=directory,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        start_new_session=sys.platform != "win32",
    )


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """只停止本脚本创建的进程组，避免遗留重载器占用端口。

    Args:
        process: 本脚本启动的进程。
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def main() -> int:
    """检查环境、安装依赖并同时运行前后端。

    Returns:
        服务失败码；环境错误为 1，用户中断为 130。
    """
    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    uv = shutil.which("uv")
    if shutil.which("node") is None or npm is None or uv is None:
        print("请先安装 Node.js 22.12+、npm 和 uv，并加入 PATH。", file=sys.stderr)
        return 1
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for port in (BACKEND_PORT, FRONTEND_PORT):
            try:
                check_port(port)
            except OSError:
                print(f"无法使用 {HOST}:{port}，请先停止旧服务或检查端口保留范围。", file=sys.stderr)
                return 1
        result = subprocess.run([uv, "sync", "--frozen"], cwd=BACKEND_DIR, check=False)
        if result.returncode != 0:
            return result.returncode
        if not (FRONTEND_DIR / "node_modules").is_dir():
            result = subprocess.run([npm, "ci"], cwd=FRONTEND_DIR, check=False)
            if result.returncode != 0:
                return result.returncode
        processes.append(
            start_process(
                [
                    uv,
                    "run",
                    "--frozen",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    HOST,
                    "--port",
                    str(BACKEND_PORT),
                    "--reload",
                ],
                BACKEND_DIR,
            )
        )
        processes.append(start_process([npm, "run", "dev"], FRONTEND_DIR))
        print(f"前端：http://{HOST}:{FRONTEND_PORT}", flush=True)
        print(f"后端：http://{HOST}:{BACKEND_PORT}/docs", flush=True)
        print("按 Ctrl+C 同时停止前后端。", flush=True)
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    print(f"服务进程已退出（退出码 {code}），停止其余服务。", flush=True)
                    return code
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\n正在停止前后端……", flush=True)
        return INTERRUPTED_EXIT_CODE
    except OSError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    finally:
        # 清理期间忽略重复中断，确保服务进程树完整退出。
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for process in reversed(processes):
                stop_process(process)
        finally:
            signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
