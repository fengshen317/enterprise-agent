"""真实 PostgreSQL 验证失败决策、退避与动作检查点的原子恢复。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import ApplicationError
from app.models.task import TaskAction, TaskActionAttempt, TaskRun
from app.schemas.task import TaskSnapshot
from app.services.task_runtime import TaskRuntime
from app.services.task_store import TaskStore
from tests.test_tasks_integration import ControlledTaskProvider
from tests.test_tasks_integration import tasks as tasks

pytestmark = pytest.mark.integration


async def prepare_runtime(app: FastAPI, provider: ControlledTaskProvider) -> tuple[TaskRuntime, TaskSnapshot]:
    """停止自动调度后手工领取，准确控制提交与中断窗口。

    Args:
        app: 专用测试库应用。
        provider: 不调用外网的模型边界。

    Returns:
        独立执行器与持有有效租约的任务。
    """
    await app.state.task_runtime.close()
    settings = app.state.task_runtime.settings.model_copy(update={"task_scheduler_enabled": False})
    runtime = TaskRuntime(app.state.database, settings, provider)
    created = await runtime.store.create(app.state.test_scope, "提交窗口验收", "window-" + uuid4().hex)
    claimed = await runtime.store.claim(runtime.owner)
    assert claimed is not None and claimed.task.id == created.task.id
    return runtime, claimed


def interrupt_after_commit(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """在真实事务提交后模拟连接确认丢失，不回滚已经提交的数据。

    Args:
        store: 待注入故障的执行账本。
        monkeypatch: 仅替换当前实例边界。
    """
    original = store.finish_action

    async def commit_then_interrupt(
        task_id: UUID,
        run_id: UUID,
        token: int,
        action_id: UUID,
        attempt_id: UUID,
        result: str | None,
        code: str | None,
        retry_at: datetime | None = None,
        pause: bool = False,
    ) -> None:
        """保存失败决策后让调用方误以为提交没有完成。

        Args:
            task_id: 任务。
            run_id: 执行。
            token: 租约。
            action_id: 逻辑请求。
            attempt_id: 实际尝试。
            result: 成功结果。
            code: 公开错误码。
            retry_at: 已计算的服务端最早重试时间。
            pause: 是否原子提交长等待暂停。
        """
        await original(task_id, run_id, token, action_id, attempt_id, result, code, retry_at, pause)
        raise SQLAlchemyError("injected commit acknowledgement loss")

    monkeypatch.setattr(store, "finish_action", commit_then_interrupt)


async def test_retry_after_and_failed_attempt_are_atomic_across_commit_loss(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 提交后崩溃也保留等待，恢复不能提前发送第二次请求。

    Args:
        tasks: 真实数据库应用与临时用户。
        monkeypatch: 在真实失败事务提交后注入中断。
    """
    app, _, provider = tasks
    runtime, snapshot = await prepare_runtime(app, provider)
    interrupt_after_commit(runtime.store, monkeypatch)
    calls = 0

    async def operation() -> str:
        """第一次临时限流，只有推进到期时间后才允许第二次成功。

        Returns:
            完整的受控结果。
        """
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "https://provider.invalid/window")
            response = httpx.Response(429, headers={"Retry-After": "120"}, request=request)
            raise httpx.HTTPStatusError("private rate limit detail", request=request, response=response)
        return "恢复后取得完整结果"

    with pytest.raises(SQLAlchemyError):
        await runtime._action(snapshot, None, "plan", {"goal": snapshot.task.goal}, operation)
    persisted = await runtime.store.get(app.state.test_scope, snapshot.task.id)
    assert persisted.run.status == "retry_wait"
    assert persisted.run.next_retry_at is not None
    assert persisted.run.next_retry_at > datetime.now(UTC) + timedelta(seconds=110)
    assert persisted.actions[0].status == "retry_wait"
    assert persisted.actions[0].attempt_count == 1 and persisted.actions[0].error_code == "RATE_LIMITED"
    assert persisted.actions[0].next_retry_at == persisted.run.next_retry_at
    async with runtime.store.factory() as session:
        attempt = await session.scalar(
            select(TaskActionAttempt).where(TaskActionAttempt.action_id == persisted.actions[0].id)
        )
        assert attempt is not None and attempt.status == "failed" and attempt.finished_at is not None
    await runtime.store.release(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, runtime.owner)
    recovered = TaskRuntime(app.state.database, runtime.settings, provider)
    assert await recovered.store.claim(recovered.owner) is None
    assert calls == 1
    # 显式推进持久化时间模拟等待已到期，测试不真实等待两分钟。
    due = datetime.now(UTC) - timedelta(seconds=1)
    async with recovered.store.factory.begin() as session:
        await session.execute(update(TaskRun).where(TaskRun.id == snapshot.run.id).values(next_retry_at=due))
        await session.execute(
            update(TaskAction).where(TaskAction.id == persisted.actions[0].id).values(next_retry_at=due)
        )
    claimed = await recovered.store.claim(recovered.owner)
    assert claimed is not None and claimed.run.fencing_token > snapshot.run.fencing_token
    result = await recovered._action(claimed, None, "plan", {"goal": snapshot.task.goal}, operation)
    assert calls == 2 and result == "恢复后取得完整结果"
    final = await recovered.store.get(app.state.test_scope, snapshot.task.id)
    assert final.actions[0].status == "succeeded" and final.actions[0].attempt_count == 2


async def test_permanent_failure_survives_crash_before_run_failure_without_resend(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """永久失败已提交但任务尚未收敛时，恢复不能再次调用供应商。

    Args:
        tasks: 专用测试数据库。
        monkeypatch: 在请求失败事务提交后模拟中断。
    """
    app, _, provider = tasks
    runtime, snapshot = await prepare_runtime(app, provider)
    interrupt_after_commit(runtime.store, monkeypatch)
    calls = 0

    async def operation() -> str:
        """始终返回永久认证拒绝，以请求次数发现错误重发。

        Returns:
            类型契约的字符串；本场景只抛出异常。
        """
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://provider.invalid/window")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("private authorization detail", request=request, response=response)

    with pytest.raises(SQLAlchemyError):
        await runtime._action(snapshot, None, "plan", {"goal": snapshot.task.goal}, operation)
    persisted = await runtime.store.get(app.state.test_scope, snapshot.task.id)
    assert persisted.run.status == "planning"
    assert persisted.actions[0].status == "failed" and persisted.actions[0].error_code == "MODEL_AUTH_DENIED"
    assert persisted.actions[0].next_retry_at is None and calls == 1
    await runtime.store.release(snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token, runtime.owner)
    recovered = TaskRuntime(app.state.database, runtime.settings, provider)
    claimed = await recovered.store.claim(recovered.owner)
    assert claimed is not None
    with pytest.raises(ApplicationError) as failed:
        await recovered._action(claimed, None, "plan", {"goal": snapshot.task.goal}, operation)
    assert failed.value.code == "MODEL_AUTH_DENIED" and calls == 1
    await recovered.store.fail(claimed.task.id, claimed.run.id, claimed.run.fencing_token, failed.value.code)
    final = await recovered.store.get(app.state.test_scope, snapshot.task.id)
    assert final.run.status == "failed" and final.actions[0].attempt_count == 1


@pytest.mark.parametrize("code", [None, "MODEL_AUTH_DENIED"])
async def test_action_outcome_advances_snapshot_version_and_event_cursor(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider], code: str | None
) -> None:
    """成功和失败提交都有新游标，使先前快照可被前端识别为过期。

    Args:
        tasks: 真实数据库应用。
        code: 成功为空，失败为永久错误码。
    """
    app, _, provider = tasks
    runtime, snapshot = await prepare_runtime(app, provider)
    task_id, run_id, token = snapshot.task.id, snapshot.run.id, snapshot.run.fencing_token
    action = await runtime.store.start_action(task_id, run_id, token, None, "plan", {"goal": snapshot.task.goal})
    attempt_id = await runtime.store.action_attempt(task_id, run_id, token, action.id)
    before = await runtime.store.get(app.state.test_scope, task_id)
    assert before.actions[0].status == "running"
    await runtime.store.finish_action(task_id, run_id, token, action.id, attempt_id, None if code else "完整结果", code)
    after = await runtime.store.get(app.state.test_scope, task_id)
    assert after.actions[0].status == ("failed" if code else "succeeded")
    assert after.run.version > before.run.version
    assert after.last_event_sequence > before.last_event_sequence
    events = await runtime.store.events(app.state.test_scope, task_id, before.last_event_sequence)
    assert events[-1].event_type == ("action_failed" if code else "action_succeeded")
