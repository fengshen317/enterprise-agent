"""定时问答任务管理接口，沿用 Cookie、CSRF 和空间鉴权。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.auth import ScopeDependency
from app.schemas.chat import RunSnapshot
from app.schemas.schedule import ScheduleInput, ScheduleView
from app.services.schedule_runtime import ScheduleRuntime

router = APIRouter(prefix="/api/schedules", tags=["定时问答"])


def runtime(request: Request) -> ScheduleRuntime:
    """获取应用持有的定时调度器。

    Args:
        request: 当前请求。

    Returns:
        当前应用的定时问答执行器。
    """
    executor: ScheduleRuntime = request.app.state.schedule_runtime
    return executor


@router.get("", response_model=list[ScheduleView])
async def listing(
    request: Request,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ScheduleView]:
    """列出当前用户在当前空间的定时任务。

    Args:
        request: 当前请求。
        scope: 已验证用户和空间。
        offset: 分页偏移。
        limit: 页面大小。

    Returns:
        按创建时间倒序的定时任务。
    """
    return await runtime(request).store.list(scope, offset, limit)


@router.post("", response_model=ScheduleView, status_code=201)
async def create(body: ScheduleInput, request: Request, scope: ScopeDependency) -> ScheduleView:
    """创建定时任务，结果写入指定对话。

    Args:
        body: 名称、问题、固定对话与 Cron。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        新建的定时任务。
    """
    executor = runtime(request)
    view = await executor.store.create(scope, body)
    executor.poke()
    return view


@router.post("/{schedule_id}", response_model=ScheduleView)
async def update(schedule_id: UUID, body: ScheduleInput, request: Request, scope: ScopeDependency) -> ScheduleView:
    """完整更新定时任务；启用时重新计算下一次触发。

    Args:
        schedule_id: 定时任务标识。
        body: 完整可编辑字段。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        更新后的定时任务。
    """
    executor = runtime(request)
    view = await executor.store.update(scope, schedule_id, body)
    executor.poke()
    return view


@router.post("/{schedule_id}/delete", status_code=204)
async def remove(schedule_id: UUID, request: Request, scope: ScopeDependency) -> Response:
    """删除定时任务，已产生的对话和运行保持不变。

    Args:
        schedule_id: 定时任务标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        空响应。
    """
    await runtime(request).store.delete(scope, schedule_id)
    return Response(status_code=204)


@router.post("/{schedule_id}/run", response_model=RunSnapshot, status_code=202)
async def run_now(schedule_id: UUID, request: Request, scope: ScopeDependency) -> RunSnapshot:
    """立即触发一次，不改变原定 Cron 的下一次时间。

    Args:
        schedule_id: 定时任务标识。
        request: 当前请求。
        scope: 已验证用户和空间。

    Returns:
        新创建的运行快照。
    """
    return await runtime(request).run_now(scope, schedule_id)
