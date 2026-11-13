"""Agent 配置与能力绑定接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.api.auth import ScopeDependency
from app.core.errors import ApplicationError
from app.schemas.agent import AgentInput, AgentUpdate, AgentView
from app.services.agents import AgentService

router = APIRouter(prefix="/api/agents", tags=["Agent"])


def service(request: Request) -> AgentService:
    """获取 Agent 服务。

    Args:
        request: 当前请求。

    Returns:
        Agent 服务。

    Raises:
        ApplicationError: 工作台未启用。
    """
    executor: AgentService | None = request.app.state.agent_service
    if executor is None:
        raise ApplicationError("AGENT_DISABLED", "Agent 工作台未启用", 503)
    return executor


@router.get("", response_model=list[AgentView])
async def list_agents(
    request: Request,
    scope: ScopeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AgentView]:
    """列出空间 Agent 与自己的私有 Agent。"""
    return await service(request).list_agents(scope, offset, limit)


@router.post("", response_model=AgentView, status_code=201)
async def create_agent(body: AgentInput, request: Request, scope: ScopeDependency) -> AgentView:
    """创建 Agent 并绑定工具、技能与知识库范围。"""
    return await service(request).create_agent(scope, body)


@router.get("/{agent_id}", response_model=AgentView)
async def get_agent(agent_id: UUID, request: Request, scope: ScopeDependency) -> AgentView:
    """读取 Agent 详情。"""
    return await service(request).get_agent(scope, agent_id)


@router.post("/{agent_id}", response_model=AgentView)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdate,
    request: Request,
    scope: ScopeDependency,
) -> AgentView:
    """完整更新 Agent，需携带期望版本。"""
    return await service(request).update_agent(scope, agent_id, body)


@router.post("/{agent_id}/archive", response_model=AgentView)
async def archive_agent(agent_id: UUID, request: Request, scope: ScopeDependency) -> AgentView:
    """归档 Agent，保留历史运行快照。"""
    return await service(request).archive_agent(scope, agent_id)
