"""技能与版本接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.api.auth import ScopeDependency
from app.core.errors import ApplicationError
from app.schemas.skill import SkillInput, SkillPublishInput, SkillVersionView, SkillView
from app.services.skills import SkillService

router = APIRouter(prefix="/api/skills", tags=["技能"])


def service(request: Request) -> SkillService:
    """获取技能服务。

    Args:
        request: 当前请求。

    Returns:
        技能服务。

    Raises:
        ApplicationError: 工作台未启用。
    """
    executor: SkillService | None = request.app.state.skill_service
    if executor is None:
        raise ApplicationError("SKILL_DISABLED", "技能功能未启用", 503)
    return executor


@router.get("", response_model=list[SkillView])
async def list_skills(
    request: Request,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[SkillView]:
    """列出空间技能与自己的私有技能。"""
    return await service(request).list_skills(scope, offset, limit)


@router.post("", response_model=SkillView, status_code=201)
async def create_skill(body: SkillInput, request: Request, scope: ScopeDependency) -> SkillView:
    """创建技能和第一版草稿。"""
    return await service(request).create_skill(scope, body)


@router.get("/{skill_id}", response_model=SkillView)
async def get_skill(skill_id: UUID, request: Request, scope: ScopeDependency) -> SkillView:
    """读取技能详情与当前版本正文。"""
    return await service(request).get_skill(scope, skill_id)


@router.post("/{skill_id}", response_model=SkillView)
async def update_skill(
    skill_id: UUID,
    body: SkillInput,
    request: Request,
    scope: ScopeDependency,
) -> SkillView:
    """新建草稿版本，不改变已绑定 Agent 的旧版本。"""
    return await service(request).update_skill(scope, skill_id, body)


@router.get("/{skill_id}/versions", response_model=list[SkillVersionView])
async def list_versions(skill_id: UUID, request: Request, scope: ScopeDependency) -> list[SkillVersionView]:
    """读取技能版本历史。"""
    return await service(request).list_versions(scope, skill_id)


@router.post("/{skill_id}/publish", response_model=SkillView)
async def publish_skill(
    skill_id: UUID,
    body: SkillPublishInput,
    request: Request,
    scope: ScopeDependency,
) -> SkillView:
    """发布指定版本。"""
    return await service(request).publish_skill(scope, skill_id, body.version)


@router.post("/{skill_id}/archive", response_model=SkillView)
async def archive_skill(skill_id: UUID, request: Request, scope: ScopeDependency) -> SkillView:
    """归档技能。"""
    return await service(request).archive_skill(scope, skill_id)
