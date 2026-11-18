"""使用受控 Python 子进程验证授权生命周期，禁止调用真实腾讯授权。"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from app.connectors import authorization
from app.connectors.authorization import CliAuthorization, _authorization_url

AUTH_URL = "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=controlled-test"


@pytest.fixture(autouse=True)
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """缩短轮询和首次等待，保持测试独立于真实授权服务。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    monkeypatch.setattr(authorization, "INITIAL_WAIT_SECONDS", 2.0)
    monkeypatch.setattr(authorization, "POLL_SECONDS", 0.01)


def make_session(monkeypatch: pytest.MonkeyPatch, script: str) -> tuple[CliAuthorization, AsyncMock, list[int]]:
    """把真实 CLI 启动替换为无凭证的受控子进程。

    Args:
        monkeypatch: pytest 补丁工具。
        script: 测试子进程脚本。

    Returns:
        会话、结束回调和创建的 PID 列表。
    """
    callback = AsyncMock()
    session = CliAuthorization("controlled-test-cli", callback)
    pids: list[int] = []

    def spawn() -> subprocess.Popen[bytes]:
        """创建可安全终止的测试进程。

        Returns:
            独立进程组中的 Python 子进程。
        """
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        pids.append(process.pid)
        return process

    monkeypatch.setattr(session, "_spawn", spawn)
    return session, callback, pids


async def test_url_returns_before_process_exit_and_reuses_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """首次返回时进程继续等扫码，重复请求共享同一进程。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    session, callback, pids = make_session(monkeypatch, f"import time; print({AUTH_URL!r}); time.sleep(30)")
    try:
        first, second = await asyncio.gather(session.start(), session.start())
        assert first["state"] == second["state"] == "awaiting_user"
        assert first["auth_url"] == second["auth_url"] == AUTH_URL
        assert first["expires_at"]
        assert len(pids) == 1
        assert session._process is not None and session._process.poll() is None
        callback.assert_not_awaited()
    finally:
        await session.close()
    assert session._process is None and session._reader is None
    assert session._task is not None and session._task.done()
    callback.assert_not_awaited()


async def test_success_finishes_and_calls_health_check_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """扫码完成只回调一次，公开结果清除短期授权链接。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    session, callback, _ = make_session(
        monkeypatch, f"import time; print({AUTH_URL!r}); time.sleep(0.15); print('Logged in successfully')"
    )
    try:
        await session.start()
        assert session._task is not None
        await asyncio.wait_for(asyncio.shield(session._task), 5)
        assert session.snapshot()["state"] == "completed"
        assert session.snapshot()["already_logged_in"] is True
        assert session.snapshot()["auth_url"] == ""
        callback.assert_awaited_once()
    finally:
        await session.close()


async def test_already_logged_in_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有登录态无需再次扫码，返回明确已登录标记。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    session, callback, _ = make_session(monkeypatch, "print('Already logged in')")
    try:
        result = await session.start()
        assert result["state"] == "completed"
        assert result["already_logged_in"] is True
        assert session._task is not None
        await session._task
        callback.assert_awaited_once()
    finally:
        await session.close()


@pytest.mark.parametrize("expires", [True, False])
async def test_timeout_and_failure_never_expose_process_output(monkeypatch: pytest.MonkeyPatch, expires: bool) -> None:
    """超时与 CLI 错误公开固定提示，回收进程并触发健康复查。

    Args:
        monkeypatch: pytest 补丁工具。
        expires: 是否模拟扫码超时。
    """
    monkeypatch.setattr(authorization, "AUTHORIZATION_TIMEOUT_SECONDS", 0.1 if expires else 10.0)
    script = "import time; print('SECRET-TOKEN'); time.sleep(30)" if expires else "raise RuntimeError('SECRET-TOKEN')"
    session, callback, _ = make_session(monkeypatch, script)
    try:
        await session.start()
        assert session._task is not None
        await asyncio.wait_for(asyncio.shield(session._task), 5)
        result = session.snapshot()
        assert result["state"] == ("expired" if expires else "failed")
        assert "SECRET-TOKEN" not in str(result)
        assert session._process is None and session._reader is None
        callback.assert_awaited_once()
    finally:
        await session.close()


async def test_missing_cli_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动失败也使用固定输出，原始系统异常不会泄漏。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    callback = AsyncMock()
    session = CliAuthorization("missing-cli", callback)

    def fail() -> subprocess.Popen[bytes]:
        """模拟系统找不到带敏感路径的命令。

        Raises:
            FileNotFoundError: 受控启动失败。
        """
        raise FileNotFoundError("private-account-secret")

    monkeypatch.setattr(session, "_spawn", fail)
    try:
        result = await session.start()
        assert result["state"] == "failed"
        assert "private-account-secret" not in str(result)
        assert session._task is not None
        await session._task
        callback.assert_awaited_once()
    finally:
        await session.close()


async def test_slow_output_returns_starting_without_cancelling_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """首次等待到期只返回处理中，继续保留授权进程。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    monkeypatch.setattr(authorization, "INITIAL_WAIT_SECONDS", 0.02)
    script = f"import time; time.sleep(0.1); print({AUTH_URL!r}); time.sleep(30)"
    session, callback, _ = make_session(monkeypatch, script)
    try:
        assert (await session.start())["state"] == "starting"
        await asyncio.wait_for(session._ready.wait(), 5)
        assert session.snapshot()["state"] == "awaiting_user"
        assert session.snapshot()["auth_url"] == AUTH_URL
    finally:
        await session.close()
    callback.assert_not_awaited()


async def test_split_authorization_code_waits_for_complete_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """授权码分块时继续等待，完整 URL 换行后才允许用户扫码。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    prefix, suffix = AUTH_URL[:-8], AUTH_URL[-8:]
    script = (
        f"import sys,time; sys.stdout.write({prefix!r}); sys.stdout.flush(); "
        f"time.sleep(0.3); print({suffix!r}); time.sleep(30)"
    )
    session, callback, _ = make_session(monkeypatch, script)
    starting = asyncio.create_task(session.start())
    try:
        await asyncio.sleep(0.15)
        assert session.snapshot()["state"] == "starting"
        assert session.snapshot()["auth_url"] == ""
        result = await asyncio.wait_for(starting, 5)
        assert result["state"] == "awaiting_user"
        assert result["auth_url"] == AUTH_URL
    finally:
        await session.close()
        await starting
    callback.assert_not_awaited()


def test_stdout_eof_accepts_complete_url_without_newline() -> None:
    """管道 EOF 可以终结完整尾 URL，兼容末行没有换行的输出。"""
    session = CliAuthorization("controlled-path", AsyncMock())
    process = Mock(stdout=io.BytesIO(AUTH_URL.encode()))
    session._read_output(process)
    session._drain_events()
    assert session.snapshot()["state"] == "awaiting_user"
    assert session.snapshot()["auth_url"] == AUTH_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=x",
        "https://evil.test/marketplace/tencentmeeting-cli-auth.html?code=x",
        "https://meeting.tencent.com.evil.test/marketplace/tencentmeeting-cli-auth.html?code=x",
        "https://meeting.tencent.com@evil.test/marketplace/tencentmeeting-cli-auth.html?code=x",
        "https://meeting.tencent.com:443/marketplace/tencentmeeting-cli-auth.html?code=x",
        "https://meeting.tencent.com/other?code=x",
        "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html",
        "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=",
        "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=%20",
        "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=x&code=y",
        "https://meeting.tencent.com/marketplace/tencentmeeting-cli-auth.html?code=x#evil",
    ],
)
def test_untrusted_urls_are_rejected(url: str) -> None:
    """授权链接必须符合精确来源、路径和非空授权码约束。

    Args:
        url: 不应公开的 URL。
    """
    assert _authorization_url(url) == ""


def test_official_url_is_extracted_from_output() -> None:
    """只保留官方链接，忽略周围日志及 ANSI 颜色。"""
    assert _authorization_url(f"private log\n\x1b[32m{AUTH_URL}\x1b[0m\n") == AUTH_URL


async def test_real_spawn_owns_descendants_and_hides_windows_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实启动逻辑接管测试进程树，关闭后读取线程不被后代管道阻塞。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"print({AUTH_URL!r}); time.sleep(30)"
    )
    process = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    popen = Mock(return_value=process)
    callback = AsyncMock()
    session = CliAuthorization("controlled-path", callback)
    monkeypatch.setattr("app.connectors.authorization.subprocess.Popen", popen)
    try:
        assert (await session.start())["state"] == "awaiting_user"
        assert popen.call_args is not None
        assert popen.call_args.args[0] == ["controlled-path", "auth", "login", "--no-browser"]
        if os.name == "nt":
            assert popen.call_args.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        await asyncio.wait_for(session.close(), 5)
        assert process.poll() is not None
        assert session._reader is None
        assert session._job_handle is None
        callback.assert_not_awaited()
    finally:
        await session.close()
        if process.poll() is None:
            process.kill()
            process.wait()


async def test_close_during_spawn_waits_for_created_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动线程尚未交回句柄时关闭，仍需回收随后创建的进程。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    session, callback, _ = make_session(monkeypatch, "import time; time.sleep(30)")
    original_spawn = session._spawn
    entered = threading.Event()
    release = threading.Event()

    def delayed_spawn() -> subprocess.Popen[bytes]:
        """在受控屏障后才交回子进程句柄。

        Returns:
            测试子进程。
        """
        entered.set()
        release.wait(5)
        return original_spawn()

    monkeypatch.setattr(session, "_spawn", delayed_spawn)
    start = asyncio.create_task(session.start())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        repeated_close = asyncio.create_task(session.close())
        release.set()
        await asyncio.wait_for(asyncio.gather(closing, repeated_close), 5)
        await start
        assert session._process is None and session._reader is None
        assert session._task is not None and session._task.done()
        callback.assert_not_awaited()
    finally:
        release.set()
        await session.close()
        await start


async def test_completed_state_waits_for_health_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """健康复查完成后才发布 completed，避免前端读取旧连接状态。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    session, _, _ = make_session(monkeypatch, f"import time; print({AUTH_URL!r}); time.sleep(0.1)")

    async def refresh() -> None:
        """模拟需要等待的连接器健康复查。"""
        entered.set()
        await release.wait()

    session._on_finished = refresh
    try:
        await session.start()
        await asyncio.wait_for(entered.wait(), 5)
        assert session.snapshot()["state"] == "awaiting_user"
        release.set()
        assert session._task is not None
        await asyncio.wait_for(session._task, 5)
        assert session.snapshot()["state"] == "completed"
    finally:
        release.set()
        await session.close()


async def test_health_refresh_failure_does_not_escape_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """健康复查失败不能产生未读取的后台异常或泄漏错误正文。

    Args:
        monkeypatch: pytest 补丁工具。
    """
    session, callback, _ = make_session(monkeypatch, "print('Already logged in')")
    callback.side_effect = RuntimeError("private-health-error")
    try:
        assert (await session.start())["state"] == "completed"
        assert session._task is not None
        await session._task
        assert session._task.exception() is None
        assert "private-health-error" not in str(session.snapshot())
        callback.assert_awaited_once()
    finally:
        await session.close()
