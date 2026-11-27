"""登录用户自己的模型配置，所有归属均由会话确定。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.auth import IdentityDependency
from app.core.errors import ApplicationError
from app.schemas.model_profiles import ModelProfileDraft, PersonalModelDraft, PersonalModelsView
from app.services.config_probe import ConfigCheck, probe_connections
from app.services.config_service import ConfigService
from app.services.model_catalog import (
    ModelCatalogRequest,
    ModelProviderPreset,
    discover_model_ids,
    list_model_providers,
)

router = APIRouter(prefix="/api/me/models", tags=["个人模型"])


def service(request: Request, response: Response) -> ConfigService:
    """复用部署加密密钥和锁，个人接口响应禁止缓存。"""
    response.headers["Cache-Control"] = "no-store"
    config: ConfigService = request.app.state.config_service
    return config


def validate_personal_endpoint(draft: ModelProfileDraft) -> None:
    """普通用户只向已知厂商发凭据，服务端限定出站地址。"""
    if draft.provider == "deepseek":
        return
    allowed = {preset.base_url.rstrip("/") for preset in list_model_providers()}
    if draft.base_url.rstrip("/") not in allowed or not draft.api_key.get_secret_value().strip():
        raise ApplicationError("PERSONAL_MODEL_INVALID", "请选择支持的厂商并填写该厂商的 API Key", 422)


async def overview(config: ConfigService, user_id: UUID) -> PersonalModelsView:
    """列表只读取该账号的配置，公共项仅返回展示信息。"""
    return PersonalModelsView(
        profiles=await config.list_model_profiles(user_id), shared_default=await config.shared_model_summary(),
    )


@router.get("", response_model=PersonalModelsView)
async def list_models(request: Request, response: Response, identity: IdentityDependency) -> PersonalModelsView:
    """列出本人模型和公共默认项。"""
    return await overview(service(request, response), identity.user_id)


@router.get("/providers", response_model=list[ModelProviderPreset])
async def providers(response: Response, identity: IdentityDependency) -> list[ModelProviderPreset]:
    """成员可复用内置厂商目录。"""
    response.headers["Cache-Control"] = "no-store"
    return list_model_providers()


@router.post("/providers/{provider_id}/models", response_model=list[str])
async def discover(
    provider_id: str, body: ModelCatalogRequest, response: Response, identity: IdentityDependency,
) -> list[str]:
    """用本次提交的个人凭据读取指定厂商目录。"""
    response.headers["Cache-Control"] = "no-store"
    return await discover_model_ids(provider_id, body.api_key)


@router.post("/test", response_model=list[ConfigCheck])
async def test_model(
    body: ModelProfileDraft, request: Request, response: Response, identity: IdentityDependency,
) -> list[ConfigCheck]:
    """测试当前输入，地址和密钥均来自经过限定的个人配置。"""
    validate_personal_endpoint(body)
    candidate = await service(request, response).settings_for_test("llm", body.config_values())
    return await probe_connections(candidate, "llm")


@router.post("", response_model=PersonalModelsView, status_code=201)
async def add_model(
    body: PersonalModelDraft, request: Request, response: Response, identity: IdentityDependency,
) -> PersonalModelsView:
    """加密保存个人配置，可原子设为本人默认。"""
    draft = body.profile()
    validate_personal_endpoint(draft)
    config = service(request, response)
    await config.add_model_profile(draft, identity.user_id, personal=True, make_default=body.make_default)
    return await overview(config, identity.user_id)


@router.post("/follow-shared", response_model=PersonalModelsView)
async def follow_shared(request: Request, response: Response, identity: IdentityDependency) -> PersonalModelsView:
    """恢复跟随空间提供的默认模型。"""
    config = service(request, response)
    await config.follow_shared_model(identity.user_id)
    return await overview(config, identity.user_id)


@router.post("/{profile_id}/activate", response_model=PersonalModelsView)
async def activate(
    profile_id: UUID, request: Request, response: Response, identity: IdentityDependency,
) -> PersonalModelsView:
    """仅切换当前账号拥有的个人模型。"""
    config = service(request, response)
    await config.activate_model_profile(profile_id, identity.user_id, personal=True)
    return await overview(config, identity.user_id)
