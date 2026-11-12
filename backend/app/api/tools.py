"""内置工具目录与空间启停策略接口。"""

from fastapi import APIRouter, Request

from app.api.agents import service as agent_service
from app.api.auth import ScopeDependency
from app.schemas.tools import ToolPolicyInput, ToolView

router = APIRouter(prefix="/api", tags=["能力目录"])


@router.get("/tools", response_model=list[ToolView])
async def list_tools(request: Request, scope: ScopeDependency) -> list[ToolView]:
    """列出当前空间可见的内置工具目录。"""
    return await agent_service(request).list_tools(scope)


@router.get("/tools/{key}", response_model=ToolView)
async def get_tool(key: str, request: Request, scope: ScopeDependency) -> ToolView:
    """读取单个内置工具目录项。"""
    return await agent_service(request).get_tool(scope, key)


@router.post("/admin/tools/{key}", response_model=ToolView)
async def set_tool_policy(
    key: str,
    body: ToolPolicyInput,
    request: Request,
    scope: ScopeDependency,
) -> ToolView:
    """由空间管理员启停内置工具。"""
    return await agent_service(request).set_tool_policy(scope, key, body.enabled)
