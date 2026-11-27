"""腾讯会议授权会话，独立管理扫码期间的进程和安全状态。"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs, urlsplit

from app.connectors.cli import resolve_cli_path

AUTHORIZATION_TIMEOUT_SECONDS = 300.0
INITIAL_WAIT_SECONDS = 8.0
POLL_SECONDS = 0.05
_OUTPUT_LIMIT = 16384
_ACTIVE_STATES = {"starting", "awaiting_user"}
_MESSAGES = {
    "starting": "正在获取腾讯会议授权链接，请稍后刷新。",
    "awaiting_user": "请打开授权链接并使用腾讯会议扫码，授权完成后会自动更新连接状态。",
    "completed": "腾讯会议授权流程已完成，正在更新连接状态。",
    "expired": "腾讯会议授权已超时，请重新发起授权。",
    "failed": "腾讯会议授权失败，请检查 CLI 安装和网络后重试。",
}


class _WindowsProcessHandle(Protocol):
    """描述 Windows Popen 持有的系统句柄，供 Job 对象接管进程树。"""

    _handle: int


def _authorization_url(text: str) -> str:
    """只提取腾讯会议官方授权页面的完整地址。

    Args:
        text: 进程输出片段。

    Returns:
        含非空授权码的官方 HTTPS 地址，无法确认时返回空字符串。
    """
    for match in re.finditer(r"https?://[^\s<>\"'\x1b]+", text):
        candidate = match.group().rstrip(").,，。")
        try:
            parsed = urlsplit(candidate)
            codes = parse_qs(parsed.query).get("code", [])
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and parsed.netloc == "meeting.tencent.com"
            and parsed.path == "/marketplace/tencentmeeting-cli-auth.html"
            and not parsed.fragment
            and len(codes) == 1
            and codes[0].strip()
        ):
            return candidate
    return ""


class CliAuthorization:
    """管理单个可复用授权进程，禁止向客户端回显 CLI 原始输出。"""

    def __init__(
        self, cli_path: str, on_finished: Callable[[], Awaitable[None]], *,
        env: dict[str, str] | None = None, cwd: Path | None = None,
    ) -> None:
        """初始化尚未启动的授权会话。

        Args:
            cli_path: CLI 命令或用户配置的完整路径。
            on_finished: 自然结束后重新检查连接器健康状态的回调。
        """
        self._cli_path = cli_path
        self._env = env
        self._cwd = cwd
        self._on_finished = on_finished
        self._state = "starting"
        self._auth_url = ""
        self._already_logged_in = False
        self._expires_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._events: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._ready = asyncio.Event()
        self._closed = False
        self._job_handle: int | None = None
        self._windows_kernel: ctypes.CDLL | None = None

    async def start(self) -> dict[str, object]:
        """启动或复用授权，拿到链接或等待八秒后返回。

        Returns:
            可安全公开的当前会话状态。
        """
        if self._closed:
            return self.snapshot()
        if self._task is None or (self._task.done() and self._state not in _ACTIVE_STATES):
            self._state = "starting"
            self._auth_url = ""
            self._already_logged_in = False
            self._ready.clear()
            self._expires_at = datetime.now(UTC) + timedelta(seconds=AUTHORIZATION_TIMEOUT_SECONDS)
            self._task = asyncio.create_task(self._run(), name="tmeet-authorization")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=INITIAL_WAIT_SECONDS)
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        """返回固定安全提示和已校验链接。

        Returns:
            授权链接、提示、登录标记、状态和过期时间。
        """
        return {
            "auth_url": self._auth_url,
            "output": _MESSAGES[self._state],
            "already_logged_in": self._already_logged_in,
            "state": self._state,
            "expires_at": self._expires_at.isoformat() if self._expires_at else None,
        }

    async def close(self) -> None:
        """取消后台任务并等待自身子进程及读取线程释放。"""
        first_close = not self._closed
        self._closed = True
        if self._task is not None and not self._task.done():
            if first_close:
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self._task)
            # 调用者自身被取消也必须等清理结束，重复关闭不能再次打断资源回收。
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._state in _ACTIVE_STATES:
            self._state = "failed"
            self._auth_url = ""
        self._ready.set()

    def _spawn(self) -> subprocess.Popen[bytes]:
        """在线程池启动 CLI，避免依赖 Windows Proactor 事件循环。

        Returns:
            当前会话拥有的子进程。
        """
        executable = resolve_cli_path(self._cli_path)
        # 默认命令使用项目原生程序，避免 npm shim 引入额外子进程和控制台。
        if self._cli_path == "tmeet" and os.name == "nt":
            native = (
                Path(__file__).resolve().parents[3]
                / "tools/node_modules/@tencentcloud/tmeet/dist/tmeet-Windows-x86_64.exe"
            )
            if native.is_file():
                executable = str(native)
        process = subprocess.Popen(
            [executable, "auth", "login", "--no-browser"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
            env=self._env,
            cwd=self._cwd,
        )
        if sys.platform == "win32":
            # Job 对象保留进程树的所有权，根进程提前退出也能关闭其后代的输出管道。
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel.CreateJobObjectW.restype = ctypes.c_void_p
            kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel.AssignProcessToJobObject.restype = ctypes.c_int
            kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel.TerminateJobObject.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.CreateJobObjectW(None, None)
            process_handle = int(cast(_WindowsProcessHandle, process)._handle)
            if not handle or not kernel.AssignProcessToJobObject(handle, process_handle):
                if handle:
                    kernel.CloseHandle(handle)
                process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                raise OSError("无法建立授权进程隔离")
            self._windows_kernel = kernel
            self._job_handle = int(handle)
        return process

    def _read_output(self, process: subprocess.Popen[bytes]) -> None:
        """用有限缓冲读取输出，仅把已校验链接和登录标记交给事件循环。

        Args:
            process: 当前授权进程。
        """
        if process.stdout is None:
            return
        stream = cast(io.BufferedReader, process.stdout)
        buffered = ""
        found_url = False
        found_login = False
        try:
            while chunk := stream.read1(4096):
                buffered = (buffered + chunk.decode("utf-8", errors="replace"))[-_OUTPUT_LIMIT:]
                # 管道可在授权码中间分块，只有完整结束的输出片段才能用于发布链接。
                complete = re.sub(r"\S+$", "", buffered)
                if not found_url and (url := _authorization_url(complete)):
                    self._events.put(("url", url))
                    found_url = True
                if not found_login and re.search(r"\b(?:already logged in|logged in successfully)\b", buffered, re.I):
                    self._events.put(("logged_in", ""))
                    found_login = True
            # EOF 明确终结最后一个片段，兼容 CLI 最后一行没有换行的输出。
            if not found_url and (url := _authorization_url(buffered)):
                self._events.put(("url", url))
        except (OSError, ValueError):
            # 关闭进程和管道会打断读取，原始异常中可能含敏感输出。
            return

    def _drain_events(self) -> None:
        """把读取线程发现的安全事件合并到当前状态。"""
        while not self._events.empty():
            kind, value = self._events.get_nowait()
            if kind == "url":
                self._auth_url = value
                self._state = "awaiting_user"
                self._ready.set()
            elif kind == "logged_in":
                self._already_logged_in = True

    async def _run(self) -> None:
        """在后台监控授权到完成、超时或失败，并确保取消后资源回收。"""
        spawn = asyncio.create_task(asyncio.to_thread(self._spawn))
        cancelled = False
        terminal_state = "failed"
        try:
            self._process = await asyncio.shield(spawn)
            self._reader = threading.Thread(
                target=self._read_output, args=(self._process,), name="tmeet-authorization-output", daemon=True
            )
            self._reader.start()
            deadline = asyncio.get_running_loop().time() + AUTHORIZATION_TIMEOUT_SECONDS
            while self._process.poll() is None:
                self._drain_events()
                if asyncio.get_running_loop().time() >= deadline:
                    terminal_state = "expired"
                    break
                await asyncio.sleep(POLL_SECONDS)
            else:
                terminal_state = "completed" if self._process.returncode == 0 else "failed"
        except asyncio.CancelledError:
            cancelled = True
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            terminal_state = "failed"
        finally:
            # shield 使启动线程即使遇到取消也能交回句柄，避免丢失已经创建的进程。
            if self._process is None:
                with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                    self._process = await spawn
            cleanup = asyncio.create_task(asyncio.to_thread(self._cleanup))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # 关闭可能恰好发生在自然退出清理期间，仍需等待线程和进程全部回收。
                cancelled = True
                await cleanup
            self._drain_events()
            if not cancelled and not self._closed:
                # 健康复查的异常不能破坏已结束会话，也不能成为未读取的后台异常。
                try:
                    await self._on_finished()
                except Exception:
                    terminal_state = "failed"
            self._state = terminal_state
            self._auth_url = ""
            self._ready.set()

    def _cleanup(self) -> None:
        """只终止当前会话进程树，等待管道读取线程真正退出。"""
        process = self._process
        if process is None:
            return
        if sys.platform == "win32":
            if self._job_handle is not None and self._windows_kernel is not None:
                self._windows_kernel.TerminateJobObject(self._job_handle, 1)
                self._windows_kernel.CloseHandle(self._job_handle)
                self._job_handle = None
                self._windows_kernel = None
            if process.poll() is None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=10,
                        check=False,
                    )
        else:
            # 独立进程组允许根进程先退出后仍回收继承输出管道的子进程。
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        process.wait()
        if self._reader is not None:
            self._reader.join()
        if process.stdout is not None:
            process.stdout.close()
        self._reader = None
        self._process = None
