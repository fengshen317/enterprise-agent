"""当前工作空间管理员接口，沿用 Cookie、Origin 和 CSRF 校验。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import ScopeDependency, SystemAdminDependency
from app.capabilities.registry import ToolRegistry
from app.core.errors import ApplicationError
from app.schemas.admin import AdminAuditView, MemberCreate, MemberPage, MemberUpdate, MemberView
from app.schemas.auth import ResourceScope
from app.services.admin import AdminService
from app.services.config_service import ConfigItem, ConfigService

router = APIRouter(prefix="/api/admin", tags=["空间成员管理"])


def service(request: Request) -> AdminService:
    """获取管理服务。

    Args:
        request: 当前应用请求。

    Returns:
        使用应用会话工厂的服务。
    """
    return AdminService(request.app.state.database.session_factory, request.app.state.settings)


@router.get("/members", response_model=MemberPage)
async def members(
    request: Request,
    response: Response,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    q: Annotated[str, Query(max_length=100)] = "",
) -> MemberPage:
    """分页列出当前空间成员。

    Args:
        request: 应用请求。
        response: 禁止缓存管理资料。
        scope: 可信身份。
        offset: 分页偏移。
        limit: 页大小。
        q: 搜索词。

    Returns:
        当前空间成员列表。
    """
    response.headers["Cache-Control"] = "no-store"
    return await service(request).members(scope, offset, limit, q)


@router.post("/members", response_model=MemberView, status_code=201)
async def create_member(body: MemberCreate, request: Request, scope: ScopeDependency) -> MemberView:
    """创建新账号并加入当前空间。

    Args:
        body: 新账号与角色。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        新成员，不包含口令或哈希。
    """
    return await service(request).create(scope, body)


@router.post("/members/{user_id}", response_model=MemberView)
async def update_member(user_id: UUID, body: MemberUpdate, request: Request, scope: ScopeDependency) -> MemberView:
    """更新当前空间成员角色与状态。

    Args:
        user_id: 目标成员。
        body: 角色与状态。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        更新后的成员。
    """
    return await service(request).update(scope, user_id, body)


@router.post("/members/{user_id}/reset-password", status_code=204)
async def reset_member_password(user_id: UUID, request: Request, scope: ScopeDependency) -> Response:
    """授权后将目标成员重置为部署的统一初始密码。"""
    await service(request).reset_password(scope, user_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/audits", response_model=list[AdminAuditView])
async def audits(
    request: Request,
    response: Response,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[AdminAuditView]:
    """分页读取当前空间的管理操作记录。

    Args:
        request: 应用请求。
        response: 禁止缓存审计资料。
        scope: 可信身份。
        offset: 分页偏移。
        limit: 页大小。

    Returns:
        安全操作快照。
    """
    response.headers["Cache-Control"] = "no-store"
    return await service(request).audits(scope, offset, limit)


@router.get("/configs", response_model=list[ConfigItem])
async def list_configs(request: Request, response: Response, identity: SystemAdminDependency) -> list[ConfigItem]:
    """列出可管理的集成配置项（密钥掩码）。

    Args:
        request: 当前请求。
        response: 禁止缓存配置。
        identity: 通过部署级鉴权的可信身份。

    Returns:
        配置项列表。
    """
    response.headers["Cache-Control"] = "no-store"
    config_service: ConfigService = request.app.state.config_service
    return await config_service.list_configs()


async def _require_admin(request: Request, scope: ResourceScope) -> None:
    """复核当前用户是空间管理员。"""
    from sqlalchemy import select

    from app.models.identity import WorkspaceMembership

    async with request.app.state.database.session_factory() as session:
        role = await session.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.workspace_id == scope.workspace_id,
                WorkspaceMembership.user_id == scope.user_id,
                WorkspaceMembership.status == "active",
            )
        )
    if role != "admin":
        raise ApplicationError("ADMIN_REQUIRED", "只有空间管理员可以管理配置", 403)


class ConfigUpdateItem(BaseModel):
    """单个配置项更新。"""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=0, max_length=10000)


class ConfigUpdateRequest(BaseModel):
    """批量更新配置。"""

    model_config = ConfigDict(extra="forbid")
    configs: list[ConfigUpdateItem] = Field(min_length=1, max_length=20)


@router.post("/configs", response_model=list[ConfigItem])
async def update_configs(
    body: ConfigUpdateRequest, request: Request, response: Response, identity: SystemAdminDependency
) -> list[ConfigItem]:
    """批量更新集成配置，保存即生效。

    Args:
        body: 待更新的配置项。
        request: 当前请求。
        response: 禁止缓存配置。
        identity: 通过部署级鉴权的可信身份。

    Returns:
        更新后的配置项列表。
    """
    config_service: ConfigService = request.app.state.config_service
    values = {item.key: item.value for item in body.configs}
    if len(values) != len(body.configs):
        raise ApplicationError("CONFIG_KEY_DUPLICATE", "同一配置项只能提交一次", 422)
    await config_service.update(values, identity.user_id)
    if any(key.startswith(("tavily_", "web_search_")) for key in values):
        registry: ToolRegistry = request.app.state.tool_registry
        registry.configure_web_search(request.app.state.settings)
    response.headers["Cache-Control"] = "no-store"
    return await config_service.list_configs()
