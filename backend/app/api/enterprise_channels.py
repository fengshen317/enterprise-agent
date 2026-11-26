"""飞书、钉钉和 QQ 官方机器人的配置与员工绑定。"""

from fastapi import APIRouter, Request, Response

from app.api.auth import ScopeDependency
from app.channels.enterprise import (
    BindCodeView,
    ChannelAppInput,
    ChannelAppView,
    ChannelBindingView,
    EnterpriseChannelService,
)

router = APIRouter(prefix="/api/channels", tags=["企业渠道"])


def service(request: Request) -> EnterpriseChannelService:
    """获取企业渠道服务。

    Args:
        request: 当前请求。

    Returns:
        应用配置和绑定码服务。
    """
    channel: EnterpriseChannelService = request.app.state.enterprise_channels
    return channel


@router.get("/apps", response_model=list[ChannelAppView])
async def list_apps(request: Request, scope: ScopeDependency) -> list[ChannelAppView]:
    """查看飞书、钉钉、QQ 是否已经配置。不返回密钥。"""
    return await service(request).list_apps(scope)


@router.post("/apps", response_model=ChannelAppView)
async def save_app(body: ChannelAppInput, request: Request, scope: ScopeDependency) -> ChannelAppView:
    """保存企业机器人。只有空间管理员可以调用。"""
    return await service(request).save_app(scope, body)


@router.post("/apps/{channel_type}/disable", status_code=204)
async def disable_app(channel_type: str, request: Request, scope: ScopeDependency) -> Response:
    """停用一种企业机器人。"""
    await service(request).disable_app(scope, channel_type)
    return Response(status_code=204)


@router.post("/{channel_type}/bind-code", response_model=BindCodeView, status_code=201)
async def bind_code(channel_type: str, request: Request, scope: ScopeDependency) -> BindCodeView:
    """为当前用户生成一次性绑定码。"""
    return await service(request).issue_code(scope, channel_type)


@router.get("/{channel_type}/binding", response_model=ChannelBindingView)
async def binding(channel_type: str, request: Request, scope: ScopeDependency) -> ChannelBindingView:
    """查看当前用户在该渠道的绑定。"""
    return await service(request).binding(scope, channel_type)


@router.post("/{channel_type}/unbind", status_code=204)
async def unbind(channel_type: str, request: Request, scope: ScopeDependency) -> Response:
    """解除当前用户的绑定。"""
    await service(request).unbind(scope, channel_type)
    return Response(status_code=204)
