"""显式任务的计划确认、控制、私有快照与重连订阅。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.auth import ScopeDependency, auth_service
from app.core.errors import ApplicationError
from app.core.security import COOKIE_NAME
from app.schemas.task import (
    PlanConfirm,
    PlanDraft,
    PlanEdit,
    TaskControl,
    TaskCreate,
    TaskEventView,
    TaskRunView,
    TaskSnapshot,
)
from app.services.task_runtime import TaskRuntime

router = APIRouter(prefix="/api/tasks", tags=["规划与任务执行"])
SSE_POLL_SECONDS = 0.5


def runtime(request: Request) -> TaskRuntime:
    """获取应用持有的执行器，读取不触发新执行。

    Args:
        request: 当前请求。

    Returns:
        应用任务执行器。
    """
    executor: TaskRuntime = request.app.state.task_runtime
    return executor


@router.post("", response_model=TaskSnapshot, status_code=202)
async def create(body: TaskCreate, request: Request, scope: ScopeDependency) -> TaskSnapshot:
    """用户显式创建任务，同键同内容不重复生成。

    Args:
        body: 目标与幂等键。
        request: 当前请求。
        scope: 可信身份。

    Returns:
        任务初始快照。
    """
    executor = runtime(request)
    snapshot = await executor.store.create(scope, body.goal, body.idempotency_key, body.source_conversation_id)
    executor.poke()
    return snapshot


@router.get("", response_model=list[TaskSnapshot])
async def listing(
    request: Request,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TaskSnapshot]:
    """列出本人当前空间的任务。

    Args:
        request: 当前请求。
        scope: 可信身份。
        offset: 偏移。
        limit: 页大小。

    Returns:
        当前用户的任务页。
    """
    return await runtime(request).store.list(scope, offset, limit)


@router.get("/{task_id}", response_model=TaskSnapshot)
async def snapshot(task_id: UUID, request: Request, scope: ScopeDependency) -> TaskSnapshot:
    """读取快照不重试模型、不唤醒执行。

    Args:
        task_id: 任务标识。
        request: 当前请求。
        scope: 可信身份。

    Returns:
        持久化状态。
    """
    return await runtime(request).store.get(scope, task_id)


@router.post("/{task_id}/confirm", response_model=TaskSnapshot)
async def confirm(task_id: UUID, body: PlanConfirm, request: Request, scope: ScopeDependency) -> TaskSnapshot:
    """只确认确切执行和不可变计划版本。

    Args:
        task_id: 任务。
        body: 执行、版本与内容摘要。
        request: 当前请求。
        scope: 可信身份。

    Returns:
        可执行快照。
    """
    executor = runtime(request)
    result = await executor.store.confirm(scope, task_id, body.revision, body.digest, body.run_id)
    executor.poke()
    return result


@router.post("/{task_id}/plan", response_model=TaskSnapshot)
async def revise(task_id: UUID, body: PlanEdit, request: Request, scope: ScopeDependency) -> TaskSnapshot:
    """追加待确认草案，不覆盖已确认内容。

    Args:
        task_id: 任务。
        body: 新计划。
        request: 当前请求。
        scope: 可信身份。

    Returns:
        新版本计划。
    """
    return await runtime(request).store.revise(scope, task_id, PlanDraft(steps=body.steps), body.run_id, body.revision)


@router.get("/{task_id}/runs", response_model=list[TaskRunView])
async def history(task_id: UUID, request: Request, scope: ScopeDependency) -> list[TaskRunView]:
    """手动重试保留旧执行记录。

    Args:
        task_id: 任务。
        request: 当前请求。
        scope: 可信身份。

    Returns:
        历次执行快照。
    """
    return await runtime(request).store.history(scope, task_id)


@router.get("/{task_id}/activity", response_model=list[TaskEventView])
async def activity(
    task_id: UUID,
    request: Request,
    scope: ScopeDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskEventView]:
    """分页重放已提交事件，不触发模型或工具。

    Args:
        task_id: 任务。
        request: 当前请求。
        scope: 可信身份。
        after_sequence: 已读取游标。

    Returns:
        安全事件页。
    """
    return await runtime(request).store.events(scope, task_id, after_sequence)


@router.get("/{task_id}/events")
async def events(
    task_id: UUID,
    request: Request,
    scope: ScopeDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """重连推送权威快照，每次轮询继续复核会话和资源权限。

    Args:
        task_id: 任务。
        request: 当前请求。
        scope: 可信身份。
        after_sequence: 客户端公开事件游标。

    Returns:
        不触发执行的 SSE。
    """
    executor = runtime(request)
    await executor.store.get(scope, task_id)

    async def stream() -> AsyncIterator[str]:
        """短事务读快照，不持有数据库会话等待。

        Yields:
            带单调游标的完整状态或撤权事件。
        """
        previous, cursor, polls = "", after_sequence, 0
        while not await request.is_disconnected():
            try:
                identity = await auth_service(request).authenticate(request.cookies.get(COOKIE_NAME, ""))
                fresh = await auth_service(request).scope(identity, scope.workspace_id)
                value = await executor.store.get(fresh, task_id)
            except ApplicationError:
                yield 'event: access_revoked\ndata: {"code":"ACCESS_REVOKED"}\n\n'
                return
            payload = value.model_dump_json()
            if payload != previous:
                cursor = max(cursor, value.last_event_sequence)
                yield f"id: {cursor}\nevent: snapshot\ndata: {payload}\n\n"
                previous = payload
            if value.run.status in ("completed", "failed", "cancelled"):
                return
            polls += 1
            if polls % 20 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


@router.post("/{task_id}/{action}", response_model=TaskSnapshot)
async def control(
    task_id: UUID,
    action: Literal["pause", "resume", "cancel", "retry"],
    body: TaskControl,
    request: Request,
    scope: ScopeDependency,
) -> TaskSnapshot:
    """版本绑定的控制请求先提交数据库，再停止飞行请求。

    Args:
        task_id: 任务。
        action: 控制动作。
        body: 当前执行与版本。
        request: 当前请求。
        scope: 可信身份。

    Returns:
        控制后的权威状态。
    """
    executor = runtime(request)
    result = await executor.store.control(scope, task_id, action, body.run_id, body.version)
    if action in ("pause", "cancel"):
        await executor.interrupt(task_id, result.run.id, result.run.fencing_token)
    else:
        executor.poke()
    return result
