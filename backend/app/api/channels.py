"""微信渠道绑定接口，沿用现有登录和空间依赖。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.auth import ScopeDependency
from app.channels.service import ChannelService
from app.schemas.channel import BindSessionView, ChannelAccountView

router = APIRouter(prefix="/api/channels/wechat", tags=["微信渠道"])


def service(request: Request) -> ChannelService:
    """获取应用持有的渠道服务。

    Args:
        request: 当前请求。

    Returns:
        微信绑定服务。
    """
    channel: ChannelService = request.app.state.channel_service
    return channel


@router.post("/bind", response_model=BindSessionView, status_code=201)
async def start_bind(request: Request, scope: ScopeDependency) -> BindSessionView:
    """为当前用户生成微信绑定二维码。

    Args:
        request: 当前请求。
        scope: 已验证的用户和空间。

    Returns:
        待扫码会话。
    """
    return await service(request).start_bind(scope)


@router.get("/bind/{session_id}", response_model=BindSessionView)
async def poll_bind(
    session_id: UUID,
    request: Request,
    scope: ScopeDependency,
    verify_code: Annotated[str | None, Query(max_length=16)] = None,
) -> BindSessionView:
    """查询扫码是否完成。

    Args:
        session_id: 绑定会话。
        request: 当前请求。
        scope: 已验证的用户和空间。
        verify_code: 手机上显示的数字。

    Returns:
        最新状态。
    """
    return await service(request).poll_bind(scope, session_id, verify_code)


@router.get("", response_model=list[ChannelAccountView])
async def listing(request: Request, scope: ScopeDependency) -> list[ChannelAccountView]:
    """列出当前用户的微信绑定。

    Args:
        request: 当前请求。
        scope: 已验证的用户和空间。

    Returns:
        未解绑的账号。
    """
    return await service(request).list_accounts(scope)


@router.post("/{account_id}/unbind", status_code=204)
async def unbind(account_id: UUID, request: Request, scope: ScopeDependency) -> Response:
    """解除当前用户的微信绑定。

    Args:
        account_id: 渠道账号。
        request: 当前请求。
        scope: 已验证的用户和空间。

    Returns:
        空响应。
    """
    await service(request).unbind(scope, account_id)
    return Response(status_code=204)
