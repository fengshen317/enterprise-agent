"""区分进程存活与数据库就绪状态。"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import SessionDependency
from app.core.errors import ApplicationError
from app.models.system import SystemMetadata

router = APIRouter(prefix="/api/health", tags=["系统健康"])
SCHEMA_VERSION = "16"


class HealthResponse(BaseModel):
    """公开健康信息，不暴露连接配置。"""

    status: Literal["ok", "ready"]
    service: str = "enterprise-agent-api"
    version: str = "0.1.0"


@router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    """确认进程可处理请求，数据库故障不影响存活判断。

    Returns:
        进程存活信息。
    """
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
async def readiness(request: Request, session: SessionDependency) -> HealthResponse:
    """检查数据库连接和迁移标记。

    Args:
        request: 当前请求。
        session: 请求级数据库会话。

    Returns:
        就绪状态。

    Raises:
        ApplicationError: 数据库不可达或迁移未完成。
    """
    try:
        async with asyncio.timeout(request.app.state.settings.database_timeout_seconds):
            version = await session.scalar(select(SystemMetadata.value).where(SystemMetadata.key == "schema_version"))
    except (SQLAlchemyError, TimeoutError, OSError) as exc:
        raise ApplicationError("DATABASE_UNAVAILABLE", "数据库未就绪，请检查连接与迁移状态", 503) from exc
    if version != SCHEMA_VERSION:
        raise ApplicationError("SCHEMA_NOT_READY", "数据库初始化版本不匹配，请执行迁移", 503)
    return HealthResponse(status="ready")
