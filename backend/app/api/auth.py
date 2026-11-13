"""登录、退出、会话及可信资源范围依赖。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.security import APIKeyCookie

from app.core.errors import ApplicationError
from app.core.security import COOKIE_NAME
from app.schemas.auth import AuthContext, LoginRequest, ResourceScope, SessionView, WorkspaceView
from app.services.auth import AuthService, validate_csrf, validate_origin

router = APIRouter(prefix="/api", tags=["登录与工作空间"])
cookie_scheme = APIKeyCookie(name=COOKIE_NAME, auto_error=False)


def auth_service(request: Request) -> AuthService:
    """获取应用会话服务。

    Args:
        request: 当前请求。

    Returns:
        当前应用身份服务。
    """
    service: AuthService = request.app.state.auth_service
    return service


async def get_identity(
    request: Request,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """业务入口统一要求会话，写入额外校验来源和 CSRF。

    Args:
        request: 当前请求。
        cookie: HttpOnly 会话令牌。
        x_csrf_token: 会话绑定的 CSRF 请求头。

    Returns:
        可信身份。
    """
    token = cookie or ""
    context = await auth_service(request).authenticate(token)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        validate_origin(request.headers.get("origin"), request.app.state.settings)
        validate_csrf(token, x_csrf_token)
    return context


IdentityDependency = Annotated[AuthContext, Depends(get_identity)]


async def get_scope(
    request: Request,
    identity: IdentityDependency,
    x_workspace_id: Annotated[str | None, Header()] = None,
    workspace_id: Annotated[str | None, Query()] = None,
) -> ResourceScope:
    """普通接口用请求头选择空间，SSE 可用公开空间 ID 查询参数。

    Args:
        request: 当前请求。
        identity: 当前身份。
        x_workspace_id: 普通请求的空间选择。
        workspace_id: SSE 使用的公开空间参数。

    Returns:
        已验证成员范围。
    """
    selected = x_workspace_id
    # 浏览器新窗口导航无法携带自定义请求头，与 SSE 一样允许查询参数选择空间。
    if request.url.path.endswith(("/events", "/file")):
        selected = workspace_id or selected
    try:
        selected_id = UUID(selected or "")
    except ValueError as exc:
        raise ApplicationError("WORKSPACE_REQUIRED", "请选择工作空间", 400) from exc
    write = request.method == "POST" and not request.url.path.endswith("/cancel")
    return await auth_service(request).scope(identity, selected_id, write)


ScopeDependency = Annotated[ResourceScope, Depends(get_scope)]


@router.post("/auth/login", response_model=SessionView)
async def login(body: LoginRequest, request: Request, response: Response) -> SessionView:
    """登录轮换 Cookie，不接受客户端指定会话令牌。

    Args:
        body: 登录凭证。
        request: 来源与客户端地址。
        response: 设置安全 Cookie。

    Returns:
        当前用户及工作空间。
    """
    settings = request.app.state.settings
    validate_origin(request.headers.get("origin"), settings)
    service = auth_service(request)
    old = request.cookies.get(COOKIE_NAME)
    token = await service.login(
        body.username, body.password.get_secret_value(), request.client.host if request.client else "unknown"
    )
    if old:
        await service.logout(old)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        path="/api",
        secure=settings.auth_cookie_secure or settings.environment == "production",
        max_age=settings.auth_session_hours * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return await service.view(await service.authenticate(token), token)


@router.get("/auth/me", response_model=SessionView)
async def current_user(request: Request, identity: IdentityDependency, response: Response) -> SessionView:
    """恢复登录状态，凭证不进入本地存储。

    Args:
        request: 当前请求。
        identity: 当前身份。
        response: 禁止缓存身份资料。

    Returns:
        当前会话。
    """
    response.headers["Cache-Control"] = "no-store"
    return await auth_service(request).view(identity, request.cookies[COOKIE_NAME])


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    """退出只撤销当前会话；重复退出不产生额外效果。

    Args:
        request: 当前请求。

    Returns:
        清除 Cookie 的响应。
    """
    validate_origin(request.headers.get("origin"), request.app.state.settings)
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        validate_csrf(token, request.headers.get("x-csrf-token"))
        await auth_service(request).logout(token)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/api", httponly=True, samesite="lax")
    return response


@router.get("/workspaces", response_model=list[WorkspaceView])
async def workspaces(request: Request, identity: IdentityDependency) -> list[WorkspaceView]:
    """列出当前用户空间。

    Args:
        request: 当前请求。
        identity: 当前身份。

    Returns:
        当前可访问空间。
    """
    return (await auth_service(request).view(identity, request.cookies[COOKIE_NAME])).workspaces
