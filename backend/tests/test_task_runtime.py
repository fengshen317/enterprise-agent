"""控制请求迟到时，只能取消它原本针对的执行器。"""

import asyncio
from uuid import uuid4

import pytest

from app.agents.task_provider import DeepSeekTaskProvider
from app.core.config import Settings
from app.core.database import Database
from app.services.task_runtime import TaskRuntime


@pytest.mark.parametrize("different_run,newer_token", [(True, False), (False, True), (False, False)])
async def test_late_interrupt_cannot_cancel_new_execution(different_run: bool, newer_token: bool) -> None:
    """暂停后立即恢复或重试，不允许旧控制回调取消新任务。

    Args:
        different_run: 是否已经切换到新的手动重试执行。
        newer_token: 同一个执行是否已经由更新租约恢复。
    """
    settings = Settings(_env_file=None, environment="test", task_scheduler_enabled=False)
    database = Database(settings)
    runtime = TaskRuntime(database, settings, DeepSeekTaskProvider(settings))
    task_id, run_id = uuid4(), uuid4()
    entered = asyncio.Event()

    async def wait() -> None:
        """模拟没有输出的外部请求。"""
        entered.set()
        await asyncio.Event().wait()

    job = asyncio.create_task(wait())
    runtime.jobs[task_id] = job
    runtime.job_runs[task_id] = uuid4() if different_run else run_id
    runtime.job_tokens[task_id] = 4 if newer_token else 1
    await entered.wait()
    try:
        await runtime.interrupt(task_id, run_id, before_token=2)
        assert job.cancelled() is (not different_run and not newer_token)
    finally:
        await runtime.close()
        await database.close()
