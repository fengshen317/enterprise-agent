"""任务状态机、执行与网络重试的真实 PostgreSQL 集成测试。"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.main import create_app
from app.models.identity import Workspace, WorkspaceMembership
from app.models.task import Task, TaskAction, TaskActionAttempt, TaskRun, TaskStep, TaskStepAttempt
from app.schemas.task import PlanDraft, PlanStep
from app.services.task_runtime import TaskRuntime
from app.services.task_store import TaskStore
from tests.auth_helpers import add_identity, cleanup_identities
from tests.test_chat_integration import ControlledProvider

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


def basic_plan() -> PlanDraft:
    """提供真实工具和依赖其结果的汇总步骤。

    Returns:
        待确认的有限计划。
    """
    return PlanDraft(
        steps=[
            PlanStep(
                key="stats", title="查询运行统计", kind="tool", tool_name="get_my_run_stats", arguments={"days": 7}
            ),
            PlanStep(
                key="summary", title="总结统计", kind="synthesis", depends_on=["stats"], instruction="根据真实统计总结"
            ),
        ]
    )


class ControlledTaskProvider:
    """只替代模型生成，数据库工具和状态转换保持真实。"""

    def __init__(self) -> None:
        """初始化可观察的模型边界与故障注入。"""
        self.draft = basic_plan()
        self.plan_calls = 0
        self.execute_calls: list[tuple[str, dict[str, str]]] = []
        self.failures: list[Exception] = []
        self.execute_failures: list[Exception] = []
        self.entered = asyncio.Event()
        self.both_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.active = 0
        self.max_active = 0

    async def plan(self, goal: str) -> PlanDraft:
        """返回计划或注入网络异常。

        Args:
            goal: 用户目标。

        Returns:
            不包含身份参数的草案。
        """
        self.plan_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.draft

    async def execute(self, goal: str, step: PlanStep, inputs: dict[str, str]) -> str:
        """只根据当前依赖生成结果，并记录并行峰值。

        Args:
            goal: 用户目标。
            step: 已确认步骤。
            inputs: 已提交上游结果。

        Returns:
            可验证的步骤正文。
        """
        self.execute_calls.append((step.key, inputs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= 2:
            self.both_entered.set()
        self.entered.set()
        try:
            await self.release.wait()
            if self.execute_failures:
                raise self.execute_failures.pop(0)
            return step.key + "结果：" + json.dumps(inputs, ensure_ascii=False)
        finally:
            self.active -= 1


@pytest.fixture
async def tasks() -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider]]:
    """在专用测试库创建临时身份，清理任务后再清理身份。

    Yields:
        真实应用、登录客户端与可控模型。
    """
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and target.database.endswith("_test")
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "EA_DATABASE_NAME": target.database},
        check=True,
    )
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "environment": "test",
            "task_scheduler_enabled": True,
            "schedule_enabled": False,
            "knowledge_enabled": False,
            "task_poll_seconds": 0.03,
            "task_heartbeat_seconds": 0.05,
            "task_retry_base_seconds": 0.01,
            "task_retry_max_seconds": 0.03,
        }
    )
    provider = ControlledTaskProvider()
    app = create_app(settings, provider=ControlledProvider(), task_provider=provider)
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                app.state.test_scope = await add_identity(app, client)
                yield app, client, provider
        finally:
            await app.state.task_runtime.close()
            await app.state.chat_runtime.close()
            if hasattr(app.state, "test_users"):
                async with app.state.database.session_factory.begin() as session:
                    await session.execute(delete(Task).where(Task.owner_user_id.in_(app.state.test_users)))
                await cleanup_identities(app)


async def create_task(client: httpx.AsyncClient, key: str = "task-key") -> dict:
    """显式创建任务，避免将普通问答当任务。

    Args:
        client: 已登录客户端。
        key: 幂等键。

    Returns:
        任务快照。
    """
    response = await client.post("/api/tasks", json={"goal": "分析最近7天的运行情况", "idempotency_key": key})
    assert response.status_code in (201, 202), response.text
    return dict(response.json())


async def wait_status(client: httpx.AsyncClient, task_id: str, statuses: set[str]) -> dict:
    """按状态轮询同步而不猜测模型执行时间。

    Args:
        client: 已登录客户端。
        task_id: 当前任务。
        statuses: 接受状态。

    Returns:
        权威快照。
    """
    async with asyncio.timeout(15):
        while True:
            response = await client.get(f"/api/tasks/{task_id}")
            assert response.status_code == 200, response.text
            body = response.json()
            if body["run"]["status"] in statuses:
                return dict(body)
            assert body["run"]["status"] not in {"failed", "cancelled"}, body
            await asyncio.sleep(0.02)


async def confirm(client: httpx.AsyncClient, snapshot: dict) -> httpx.Response:
    """确认精确执行与计划版本。

    Args:
        client: 已登录客户端。
        snapshot: 待确认快照。

    Returns:
        HTTP 响应。
    """
    return await client.post(
        f"/api/tasks/{snapshot['task']['id']}/confirm",
        json={
            "run_id": snapshot["run"]["id"],
            "revision": snapshot["plan"]["revision"],
            "digest": snapshot["plan"]["digest"],
        },
    )


async def control(client: httpx.AsyncClient, snapshot: dict, action: str) -> httpx.Response:
    """控制请求绑定执行版本，避免旧页面操纵新执行。

    Args:
        client: 已登录客户端。
        snapshot: 当前状态。
        action: 固定控制操作。

    Returns:
        HTTP 响应。
    """
    return await client.post(
        f"/api/tasks/{snapshot['task']['id']}/{action}",
        json={
            "run_id": snapshot["run"]["id"],
            "version": snapshot["run"]["version"],
        },
    )


async def test_explicit_task_plan_confirmation_and_real_tool_execution(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """确认前不执行工具，任务统计与问答分离，结束后 SSE 可重放。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    created = await create_task(client)
    task_id = created["task"]["id"]
    draft = await wait_status(client, task_id, {"awaiting_confirmation"})
    assert provider.plan_calls == 1 and not provider.execute_calls
    assert draft["steps"] == []
    assert (await client.get("/api/conversations")).json() == []
    same = await create_task(client)
    assert same["task"]["id"] == task_id
    assert (
        await client.post("/api/tasks", json={"goal": "不同目标", "idempotency_key": "task-key"})
    ).status_code == 409
    response = await confirm(client, draft)
    assert response.status_code in (200, 202), response.text
    finished = await wait_status(client, task_id, {"completed"})
    assert all(step["status"] == "succeeded" for step in finished["steps"])
    stats = json.loads(finished["steps"][0]["result"])
    assert stats["total"] == 0
    assert provider.execute_calls[0][0] == "summary" and "stats" in provider.execute_calls[0][1]
    stream = await client.get(f"/api/tasks/{task_id}/events")
    assert stream.status_code == 200 and "event: snapshot" in stream.text
    assert "completed" in stream.text
    assert provider.plan_calls == 1 and len(provider.execute_calls) == 1
    async with app.state.database.session_factory() as session:
        action_ids = select(TaskAction.id).where(TaskAction.run_id == UUID(finished["run"]["id"]))
        attempts = await session.scalar(
            select(func.count()).select_from(TaskActionAttempt).where(TaskActionAttempt.action_id.in_(action_ids))
        )
        assert attempts == 3


async def test_task_ownership_and_csrf_protect_all_control_paths(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """空间管理员也不能查看或控制他人的私有任务。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, _ = tasks
    snapshot = await create_task(client)
    task_id = snapshot["task"]["id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other") as other:
        async with app.state.database.session_factory() as session:
            space = await session.get(Workspace, app.state.test_scope.workspace_id)
            assert space is not None
            slug = space.slug
        await add_identity(app, other, workspace=slug, role="admin")
        assert (await other.get("/api/tasks")).json() == []
        for path in (f"/api/tasks/{task_id}", f"/api/tasks/{task_id}/events"):
            assert (await other.get(path)).status_code == 404
        for action in ("pause", "resume", "cancel", "retry"):
            assert (await control(other, snapshot, action)).status_code == 404
    assert (
        await client.post(
            "/api/tasks", json={"goal": "伪造", "idempotency_key": "spoof", "owner_user_id": str(uuid4())}
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/tasks", json={"goal": "无权", "idempotency_key": "csrf"}, headers={"X-CSRF-Token": "wrong"}
        )
    ).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://anonymous") as anonymous:
        assert (await anonymous.get(f"/api/tasks/{task_id}")).status_code == 401


async def test_transient_plan_failure_retries_without_recreating_task(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """503 只增加请求尝试，不创建第二个逻辑任务或执行。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    request = httpx.Request("POST", "https://provider.invalid/test")
    response = httpx.Response(503, request=request)
    provider.failures = [httpx.HTTPStatusError("private-provider", request=request, response=response)]
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert provider.plan_calls == 2 and draft["run"]["id"] == created["run"]["id"]
    async with app.state.database.session_factory() as session:
        action_ids = select(TaskAction.id).where(TaskAction.run_id == UUID(draft["run"]["id"]))
        statuses = list(
            await session.scalars(select(TaskActionAttempt.status).where(TaskActionAttempt.action_id.in_(action_ids)))
        )
        assert "failed" in statuses and "completed" in statuses
    assert "private-provider" not in json.dumps(draft)


async def test_permanent_failure_and_manual_retry_preserve_run_history(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """权限/凭证类故障不自动重试，用户重试则新建执行而非覆盖旧行。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    request = httpx.Request("POST", "https://provider.invalid/test")
    response = httpx.Response(403, request=request)
    provider.failures = [httpx.HTTPStatusError("private", request=request, response=response)]
    created = await create_task(client)
    failed = await wait_status(client, created["task"]["id"], {"failed"})
    assert provider.plan_calls == 1
    retried = await control(client, failed, "retry")
    assert retried.status_code in (200, 202), retried.text
    fresh = retried.json()
    assert fresh["run"]["id"] != failed["run"]["id"]
    await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    async with app.state.database.session_factory() as session:
        old = await session.get(TaskRun, UUID(failed["run"]["id"]))
        assert old is not None and old.status == "failed"
    assert (await control(client, failed, "cancel")).status_code == 409


async def test_pause_cancel_and_stale_control_cannot_publish_late_result(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """暂停/取消阻止迟到提交，旧版本不能操作新状态。

    Args:
        tasks: 真实数据库应用。
    """
    _, client, provider = tasks
    provider.release.clear()
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert (await confirm(client, draft)).status_code in (200, 202)
    await asyncio.wait_for(provider.entered.wait(), 5)
    active = (await client.get(f"/api/tasks/{created['task']['id']}")).json()
    paused = await control(client, active, "pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["run"]["status"] == "paused"
    assert (await control(client, active, "resume")).status_code == 409
    cancelled = await control(client, paused.json(), "cancel")
    assert cancelled.status_code == 200 and cancelled.json()["run"]["status"] == "cancelled"
    provider.release.set()
    final = (await client.get(f"/api/tasks/{created['task']['id']}")).json()
    assert final["run"]["status"] == "cancelled" and final["run"]["result"] is None


async def test_store_fencing_and_action_checkpoint_survive_lease_recovery(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """成功动作和未完成步骤分开提交，恢复不重放已成功动作。

    Args:
        tasks: 真实数据库应用。
    """
    app, _, _ = tasks
    await app.state.task_runtime.close()
    store = TaskStore(app.state.database.session_factory)
    snapshot = await store.create(app.state.test_scope, "检查点恢复", "store-checkpoint")
    claimed = await store.claim("old-process")
    assert claimed is not None and claimed.task.id == snapshot.task.id
    await store.save_plan(snapshot.task.id, claimed.run.id, claimed.run.fencing_token, basic_plan())
    planned = await store.get(app.state.test_scope, snapshot.task.id)
    assert planned.plan is not None
    await store.confirm(app.state.test_scope, snapshot.task.id, planned.plan.revision, planned.plan.digest)
    executing = await store.claim("old-process")
    assert executing is not None
    token = executing.run.fencing_token
    await store.start_step(snapshot.task.id, executing.run.id, token, "stats")
    action = await store.start_action(snapshot.task.id, executing.run.id, token, "stats", "tool", {"days": 7})
    attempt_id = await store.action_attempt(snapshot.task.id, executing.run.id, token, action.id)
    await store.finish_action(snapshot.task.id, executing.run.id, token, action.id, attempt_id, '{"total":0}', None)
    await store.release(snapshot.task.id, executing.run.id, token, "old-process")
    recovered = await store.claim("new-process")
    assert recovered is not None and recovered.run.fencing_token > token
    with pytest.raises(ApplicationError) as stale:
        await store.complete_step(snapshot.task.id, executing.run.id, token, "stats", "迟到结果")
    assert stale.value.code == "TASK_LEASE_LOST"
    await store.start_step(snapshot.task.id, recovered.run.id, recovered.run.fencing_token, "stats")
    checkpoint = await store.start_action(
        snapshot.task.id, recovered.run.id, recovered.run.fencing_token, "stats", "tool", {"days": 7}
    )
    assert checkpoint.status == "succeeded" and checkpoint.result == '{"total":0}'
    assert checkpoint.attempt_count == 1
    async with app.state.database.session_factory() as session:
        attempts = list(await session.scalars(select(TaskStepAttempt.status)))
        assert "interrupted" in attempts


async def test_waiting_deadline_and_membership_rechecked_after_restart(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """等待到期、恢复前过期或撤权均不能继续发送外部请求。

    Args:
        tasks: 真实数据库应用。
    """
    app, _, _ = tasks
    await app.state.task_runtime.close()
    store = TaskStore(app.state.database.session_factory)
    snapshot = await store.create(app.state.test_scope, "截止时间", "deadline")
    claimed = await store.claim("process")
    assert claimed is not None
    action = await store.start_action(snapshot.task.id, claimed.run.id, claimed.run.fencing_token, None, "plan", {})
    with pytest.raises(ApplicationError) as overdue:
        await store.retry_wait(
            snapshot.task.id,
            claimed.run.id,
            claimed.run.fencing_token,
            None,
            action.id,
            datetime.now(UTC) + timedelta(days=1),
            "HTTP_503",
        )
    assert overdue.value.code == "DEADLINE_EXCEEDED"
    async with app.state.database.session_factory.begin() as session:
        await session.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.run.id)
            .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1), lease_owner=None, lease_expires_at=None)
        )
    assert await store.claim("replacement") is None
    assert (await store.get(app.state.test_scope, snapshot.task.id)).run.error_code == "DEADLINE_EXCEEDED"
    second = await store.create(app.state.test_scope, "撤权", "revoked")
    async with app.state.database.session_factory.begin() as session:
        await session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == app.state.test_scope.workspace_id,
                WorkspaceMembership.user_id == app.state.test_scope.user_id,
            )
            .values(status="revoked")
        )
    assert await store.claim("replacement") is None
    async with app.state.database.session_factory() as session:
        run = await session.get(TaskRun, second.run.id)
        assert run is not None and run.error_code == "ACCESS_REVOKED"


async def test_parallel_ready_steps_wait_for_both_before_synthesis(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """仅独立分支可并行，汇总必须等待所有依赖提交成功。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    app.state.task_runtime.settings.task_parallel_steps = 2
    provider.draft = PlanDraft(
        steps=[
            PlanStep(key="a", title="独立分析甲", kind="analysis"),
            PlanStep(key="b", title="独立分析乙", kind="analysis"),
            PlanStep(key="merge", title="汇总", kind="synthesis", depends_on=["a", "b"]),
        ]
    )
    provider.release.clear()
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert (await confirm(client, draft)).status_code in (200, 202)
    await asyncio.wait_for(provider.both_entered.wait(), 5)
    assert provider.max_active == 2
    assert {key for key, _ in provider.execute_calls} == {"a", "b"}
    provider.release.set()
    finished = await wait_status(client, created["task"]["id"], {"completed"})
    assert all(step["status"] == "succeeded" for step in finished["steps"])
    assert provider.execute_calls[-1][0] == "merge"
    assert set(provider.execute_calls[-1][1]) == {"a", "b"}


async def test_model_retry_reuses_completed_tool_action(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """下游生成失败重试时，已经成功的查询不会被重新执行。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    provider.execute_failures = [httpx.ReadTimeout("private-stream-fragment")]
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert (await confirm(client, draft)).status_code in (200, 202)
    final = await wait_status(client, created["task"]["id"], {"completed"})
    assert len(provider.execute_calls) == 2
    assert "private-stream" not in json.dumps(final)
    async with app.state.database.session_factory() as session:
        tool_actions = list(
            await session.scalars(
                select(TaskAction).where(TaskAction.run_id == UUID(final["run"]["id"]), TaskAction.kind == "tool")
            )
        )
        assert len(tool_actions) == 1 and tool_actions[0].attempt_count == 1
        tool_attempts = await session.scalar(
            select(func.count()).select_from(TaskActionAttempt).where(TaskActionAttempt.action_id == tool_actions[0].id)
        )
        assert tool_attempts == 1


async def test_cancel_while_waiting_does_not_bypass_retry_after(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """等待记录真实持久化，取消后不能启动下一次请求。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    request = httpx.Request("POST", "https://provider.invalid/test")
    response = httpx.Response(429, request=request, headers={"Retry-After": "20"})
    provider.failures = [httpx.HTTPStatusError("private-rate-limit", request=request, response=response)]
    created = await create_task(client)
    waiting = await wait_status(client, created["task"]["id"], {"retry_wait"})
    assert waiting["run"]["next_retry_at"] is not None and provider.plan_calls == 1
    cancelled = await control(client, waiting, "cancel")
    assert cancelled.status_code == 200, cancelled.text
    await app.state.task_runtime.close()
    async with app.state.database.session_factory() as session:
        action_ids = select(TaskAction.id).where(TaskAction.run_id == UUID(waiting["run"]["id"]))
        count = await session.scalar(
            select(func.count()).select_from(TaskActionAttempt).where(TaskActionAttempt.action_id.in_(action_ids))
        )
        assert count == 1
    assert provider.plan_calls == 1


async def test_plan_revision_invalidates_old_confirmation(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """计划修改追加版本，旧确认不能悄悄执行新内容。

    Args:
        tasks: 真实数据库应用。
    """
    _, client, provider = tasks
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    revised = basic_plan().model_dump(mode="json")
    revised["steps"][0]["arguments"] = {"days": 14}
    revised["run_id"] = draft["run"]["id"]
    revised["revision"] = draft["plan"]["revision"]
    response = await client.post(f"/api/tasks/{created['task']['id']}/plan", json=revised)
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["plan"]["revision"] == draft["plan"]["revision"] + 1
    assert new["plan"]["digest"] != draft["plan"]["digest"]
    assert (await client.post(f"/api/tasks/{created['task']['id']}/plan", json=revised)).status_code == 409
    assert (
        await client.post(
            f"/api/tasks/{created['task']['id']}/plan",
            json={**revised, "run_id": str(uuid4()), "revision": new["plan"]["revision"]},
        )
    ).status_code == 409
    assert (await confirm(client, draft)).status_code == 409
    assert not provider.execute_calls
    assert (await confirm(client, new)).status_code in (200, 202)
    final = await wait_status(client, created["task"]["id"], {"completed"})
    assert json.loads(final["steps"][0]["result"])["days"] == 14
    assert (await client.post(f"/api/tasks/{created['task']['id']}/plan", json=revised)).status_code == 409


async def test_long_retry_after_pause_resume_preserves_earliest_request_time(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """长等待进入暂停，恢复只回到等待而非提前请求供应商。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    app.state.task_runtime.settings.task_retry_max_wait_seconds = 0.05
    request = httpx.Request("POST", "https://provider.invalid/test")
    response = httpx.Response(429, request=request, headers={"Retry-After": "20"})
    provider.failures = [httpx.HTTPStatusError("private", request=request, response=response)]
    created = await create_task(client)
    paused = await wait_status(client, created["task"]["id"], {"paused"})
    retry_at = paused["run"]["next_retry_at"]
    assert retry_at is not None
    assert datetime.fromisoformat(retry_at) > datetime.now(UTC) + timedelta(seconds=10)
    resumed = await control(client, paused, "resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run"]["status"] == "retry_wait"
    assert resumed.json()["run"]["next_retry_at"] == retry_at
    assert provider.plan_calls == 1
    current = (await client.get(f"/api/tasks/{created['task']['id']}")).json()
    cancelled = await control(client, current, "cancel")
    assert cancelled.status_code == 200 and provider.plan_calls == 1


async def test_retry_after_beyond_deadline_fails_without_early_retry(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """不能截短供应商长等待，也不能越过总截止时间继续执行。

    Args:
        tasks: 真实数据库应用。
    """
    _, client, provider = tasks
    request = httpx.Request("POST", "https://provider.invalid/test")
    response = httpx.Response(429, request=request, headers={"Retry-After": "3600"})
    provider.failures = [httpx.HTTPStatusError("private", request=request, response=response)]
    created = await create_task(client)
    final = await wait_status(client, created["task"]["id"], {"failed"})
    assert final["run"]["error_code"] == "DEADLINE_EXCEEDED"
    assert provider.plan_calls == 1


async def test_runtime_restart_auto_recovers_same_run_without_repeating_tool(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """真正关闭并新建调度器，恢复不依赖用户再发起任务请求。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    provider.release.clear()
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert (await confirm(client, draft)).status_code == 200
    await asyncio.wait_for(provider.entered.wait(), 5)
    interrupted = (await client.get(f"/api/tasks/{created['task']['id']}")).json()
    assert interrupted["steps"][0]["status"] == "succeeded"
    original = app.state.task_runtime
    await original.close()
    provider.entered.clear()
    replacement = TaskRuntime(app.state.database, app.state.settings, provider)
    app.state.task_runtime = replacement
    await replacement.start()
    # 尚未访问HTTP快照，后台启动扫描应自行恢复执行。
    await asyncio.wait_for(provider.entered.wait(), 5)
    assert len(provider.execute_calls) == 2
    provider.release.set()
    final = await wait_status(client, created["task"]["id"], {"completed"})
    assert final["run"]["id"] == interrupted["run"]["id"]
    assert final["run"]["fencing_token"] > interrupted["run"]["fencing_token"]
    assert provider.plan_calls == 1
    async with app.state.database.session_factory() as session:
        tools = list(
            await session.scalars(
                select(TaskAction).where(TaskAction.run_id == UUID(final["run"]["id"]), TaskAction.kind == "tool")
            )
        )
        assert len(tools) == 1 and tools[0].attempt_count == 1
        action_ids = select(TaskAction.id).where(TaskAction.run_id == UUID(final["run"]["id"]))
        attempt_status = list(
            await session.scalars(select(TaskActionAttempt.status).where(TaskActionAttempt.action_id.in_(action_ids)))
        )
        assert "interrupted" in attempt_status and "running" not in attempt_status


async def test_parallel_failure_closes_other_inflight_action_attempt(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个并行分支失败后，另一分支不能永久遗留运行中审计。

    Args:
        tasks: 真实数据库应用。
        monkeypatch: 注入分支的确定性失败顺序。
    """
    app, client, provider = tasks
    app.state.task_runtime.settings.task_parallel_steps = 2
    provider.draft = PlanDraft(
        steps=[
            PlanStep(key="a", title="分支甲", kind="analysis"),
            PlanStep(key="b", title="分支乙", kind="analysis"),
            PlanStep(key="merge", title="汇总", kind="synthesis", depends_on=["a", "b"]),
        ]
    )
    other_started = asyncio.Event()

    async def failing_branch(goal: str, step: PlanStep, inputs: dict[str, str]) -> str:
        """确保另一个请求已飞行，再让当前分支失败。

        Args:
            goal: 目标。
            step: 当前分支。
            inputs: 上游资料。

        Returns:
            本场景不会正常返回。
        """
        if step.key == "a":
            await other_started.wait()
            raise ApplicationError("BRANCH_DENIED", "测试分支失败", 403)
        other_started.set()
        await asyncio.Event().wait()
        return "不可到达"

    monkeypatch.setattr(provider, "execute", failing_branch)
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert (await confirm(client, draft)).status_code == 200
    final = await wait_status(client, created["task"]["id"], {"failed"})
    assert final["run"]["error_code"] == "BRANCH_DENIED"
    assert not any(step["status"] == "succeeded" for step in final["steps"])
    async with app.state.database.session_factory() as session:
        action_ids = select(TaskAction.id).where(TaskAction.run_id == UUID(final["run"]["id"]))
        statuses = list(
            await session.scalars(select(TaskActionAttempt.status).where(TaskActionAttempt.action_id.in_(action_ids)))
        )
        assert "failed" in statuses and "running" not in statuses
        other_ids = select(TaskAction.id).where(
            TaskAction.run_id == UUID(final["run"]["id"]), TaskAction.step_key == "b"
        )
        other_statuses = list(
            await session.scalars(select(TaskActionAttempt.status).where(TaskActionAttempt.action_id.in_(other_ids)))
        )
        assert other_statuses and "running" not in other_statuses


async def test_dynamic_membership_revoke_stops_silent_model_request(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """模型无输出时也持续授权检查，撤权不能留下飞行请求和迟到结果。

    Args:
        tasks: 真实数据库应用。
    """
    app, client, provider = tasks
    provider.release.clear()
    created = await create_task(client)
    draft = await wait_status(client, created["task"]["id"], {"awaiting_confirmation"})
    assert (await confirm(client, draft)).status_code == 200
    await asyncio.wait_for(provider.entered.wait(), 5)
    async with app.state.database.session_factory.begin() as session:
        await session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == app.state.test_scope.workspace_id,
                WorkspaceMembership.user_id == app.state.test_scope.user_id,
            )
            .values(status="revoked")
        )
    assert (await client.get(f"/api/tasks/{created['task']['id']}")).status_code == 403
    async with asyncio.timeout(5):
        while True:
            async with app.state.database.session_factory() as session:
                row = await session.get(TaskRun, UUID(created["run"]["id"]))
                assert row is not None
                if row.status == "failed":
                    assert row.error_code in {"ACCESS_REVOKED", "WORKSPACE_ACCESS_DENIED"}
                    assert row.result is None
                    break
            await asyncio.sleep(0.02)
    provider.release.set()
    async with app.state.database.session_factory() as session:
        action_ids = select(TaskAction.id).where(TaskAction.run_id == UUID(created["run"]["id"]))
        statuses = list(
            await session.scalars(select(TaskActionAttempt.status).where(TaskActionAttempt.action_id.in_(action_ids)))
        )
        assert "running" not in statuses


async def test_database_rejects_cross_task_run_and_invalid_ledger_states(
    tasks: tuple[FastAPI, httpx.AsyncClient, ControlledTaskProvider],
) -> None:
    """数据库拒绝跨任务执行指针、缺失结果与不完整尝试终态。

    Args:
        tasks: 专用真实测试数据库应用。
    """
    app, _, _ = tasks
    await app.state.task_runtime.close()
    store = TaskStore(app.state.database.session_factory)
    first = await store.create(app.state.test_scope, "约束任务一", "constraints-one")
    second = await store.create(app.state.test_scope, "约束任务二", "constraints-two")
    claim = await store.claim("constraint-process")
    assert claim is not None and claim.task.id == first.task.id
    await store.save_plan(first.task.id, claim.run.id, claim.run.fencing_token, basic_plan())
    planned = await store.get(app.state.test_scope, first.task.id)
    assert planned.plan is not None
    await store.confirm(app.state.test_scope, first.task.id, planned.plan.revision, planned.plan.digest)
    claim = await store.claim("constraint-process")
    assert claim is not None and claim.task.id == first.task.id
    await store.start_step(first.task.id, claim.run.id, claim.run.fencing_token, "stats")
    action = await store.start_action(first.task.id, claim.run.id, claim.run.fencing_token, "stats", "tool", {})
    attempt_id = await store.action_attempt(first.task.id, claim.run.id, claim.run.fencing_token, action.id)
    async with app.state.database.session_factory.begin() as session:
        # 该复合外键允许事务内先创建Task、再创建Run，只在提交边界强制一致。
        async with session.begin_nested() as savepoint:
            await session.execute(update(Task).where(Task.id == first.task.id).values(current_run_id=second.run.id))
            with pytest.raises(IntegrityError):
                await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            await savepoint.rollback()
        step_id = await session.scalar(
            select(TaskStep.id).where(TaskStep.run_id == claim.run.id, TaskStep.key == "stats")
        )
        assert step_id is not None
        invalid = [
            update(TaskAction).where(TaskAction.id == action.id).values(status="succeeded", result=None),
            insert(TaskActionAttempt).values(action_id=action.id, attempt_no=0),
            update(TaskActionAttempt).where(TaskActionAttempt.id == attempt_id).values(status="completed"),
            insert(TaskStepAttempt).values(step_id=step_id, attempt_no=0),
            update(TaskStep).where(TaskStep.id == step_id).values(status="succeeded", result=None),
            update(TaskRun).where(TaskRun.id == claim.run.id).values(status="completed", finished_at=None),
            update(TaskRun).where(TaskRun.id == claim.run.id).values(lease_owner=None),
        ]
        for statement in invalid:
            async with session.begin_nested() as savepoint:
                with pytest.raises(IntegrityError):
                    await session.execute(statement)
                await savepoint.rollback()
        assert await session.scalar(select(Task.current_run_id).where(Task.id == first.task.id)) == first.run.id
