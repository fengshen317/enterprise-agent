"""当前工作空间管理员的诊断日志查询接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.auth import ScopeDependency
from app.core.database import Database, SessionDependency
from app.schemas.system_log import SystemLogDetail, SystemLogFilters, SystemLogPage
from app.services.admin import AdminService
from app.services.system_logs import get_system_log, list_system_logs

router = APIRouter(prefix="/api/admin/system-logs", tags=["系统日志"])


@router.get("", response_model=SystemLogPage)
async def list_events(
    request: Request,
    response: Response,
    scope: ScopeDependency,
    session: SessionDependency,
    filters: Annotated[SystemLogFilters, Query()],
) -> SystemLogPage:
    """管理员在当前工作空间筛选并分页读取日志。

    Args:
        request: 当前应用请求。
        response: 禁止缓存管理资料。
        scope: 经过会话验证的资源范围。
        session: 本次查询的独立会话。
        filters: 筛选和分页条件。

    Returns:
        当前空间事件和筛选后的统计。
    """
    database: Database = request.app.state.database
    await AdminService(database.session_factory, request.app.state.settings).authorize(scope)
    response.headers["Cache-Control"] = "no-store"
    return await list_system_logs(session, scope.workspace_id, filters)


@router.get("/{event_id}", response_model=SystemLogDetail)
async def event_detail(
    event_id: UUID,
    request: Request,
    response: Response,
    scope: ScopeDependency,
    session: SessionDependency,
) -> SystemLogDetail:
    """管理员读取一条诊断事件及其关联记录。

    Args:
        event_id: 诊断编号。
        request: 当前应用请求。
        response: 禁止缓存管理资料。
        scope: 经过会话验证的资源范围。
        session: 本次查询的独立会话。

    Returns:
        当前空间中的脱敏事件和时间线。
    """
    database: Database = request.app.state.database
    await AdminService(database.session_factory, request.app.state.settings).authorize(scope)
    response.headers["Cache-Control"] = "no-store"
    return await get_system_log(session, scope.workspace_id, event_id)
