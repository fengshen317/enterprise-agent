"""个人 CLI 的临时凭据目录不能早于持有它的线程释放。"""

import asyncio
from threading import Event

import pytest

from app.connectors.personal_cli import finish_thread


async def test_cancel_waits_for_worker_before_returning() -> None:
    """请求被取消时，清理目录的上层仍会等到 CLI 真正退出。"""
    loop = asyncio.get_running_loop()
    started, stopped = asyncio.Event(), asyncio.Event()
    release = Event()

    def worker() -> str:
        """模拟执行固定命令期间仍需访问个人凭据。"""
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=3)
        loop.call_soon_threadsafe(stopped.set)
        return "finished"

    task = asyncio.create_task(finish_thread(worker))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
