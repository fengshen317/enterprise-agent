"""部署级知识库模型管理，沿用工作台登录与 CSRF。"""

from fastapi import APIRouter, Request, Response

from app.api.auth import SystemAdminDependency
from app.core.errors import ApplicationError
from app.schemas.ragflow_models import EmbeddingSetup, EmbeddingStatus
from app.services.ragflow_models import RagflowModelService

router = APIRouter(prefix="/api/admin/knowledge-model", tags=["知识库模型管理"])


def model_service(request: Request) -> RagflowModelService | None:
    """获取当前部署的模型服务。"""
    result: RagflowModelService | None = request.app.state.ragflow_model_service
    return result


@router.get("", response_model=EmbeddingStatus)
async def get_embedding_status(
    request: Request, response: Response, identity: SystemAdminDependency
) -> EmbeddingStatus:
    """提供连接状态和当前默认模型信息。"""
    response.headers["Cache-Control"] = "no-store"
    service = model_service(request)
    return await service.status() if service is not None else EmbeddingStatus(state="not_connected")


@router.post("", response_model=EmbeddingStatus)
async def configure_embedding(
    body: EmbeddingSetup, request: Request, response: Response, identity: SystemAdminDependency
) -> EmbeddingStatus:
    """验证并应用候选 Embedding 模型。"""
    response.headers["Cache-Control"] = "no-store"
    service = model_service(request)
    if service is None:
        raise ApplicationError("RAGFLOW_NOT_CONFIGURED", "RAGFlow 服务账号尚未就绪", 503)
    return await service.configure(body)
