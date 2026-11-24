"""首个管理员的同源部署入口。"""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.services.auth import validate_origin
from app.services.initial_setup import MAX_TOKEN_LENGTH, MIN_TOKEN_LENGTH, InitialSetupService

router = APIRouter(prefix="/api/auth/setup", tags=["首次部署"])


class SetupStatus(BaseModel):
    """浏览器可见的初始化状态和预设用户名。"""

    available: bool
    username: str | None = None


class SetupRequest(BaseModel):
    """部署口令和待创建账号的输入。"""

    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=MIN_TOKEN_LENGTH, max_length=MAX_TOKEN_LENGTH)
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    password: SecretStr


class SetupResult(BaseModel):
    """创建后的公开登录名。"""

    username: str


@router.get("", response_model=SetupStatus)
async def status(request: Request, response: Response) -> SetupStatus:
    """让登录页切换到首次初始化表单。"""
    response.headers["Cache-Control"] = "no-store"
    service: InitialSetupService = request.app.state.initial_setup
    available = await service.available()
    return SetupStatus(available=available, username=service.username if available else None)


@router.post("", response_model=SetupResult, status_code=201)
async def create(body: SetupRequest, request: Request, response: Response) -> SetupResult:
    """用部署口令授权首次创建，复用工作台可信来源校验。"""
    response.headers["Cache-Control"] = "no-store"
    validate_origin(request.headers.get("origin"), request.app.state.settings)
    service: InitialSetupService = request.app.state.initial_setup
    username = await service.create_admin(
        body.token.get_secret_value(), body.username, body.display_name, body.password.get_secret_value()
    )
    return SetupResult(username=username)
