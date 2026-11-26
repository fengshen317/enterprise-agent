"""登录、退出、会话及可信资源范围依赖。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.security import APIKeyCookie

from app.core.errors import ApplicationError
from app.core.security import COOKIE_NAME
from app.schemas.auth import AuthContext, LoginRequest, PasswordChange, ResourceScope, SessionView, WorkspaceView
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


async def get_session_identity(
    request: Request,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """验证会话及写入来源，允许使用初始密码的会话访问改密入口。

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


SessionIdentityDependency = Annotated[AuthContext, Depends(get_session_identity)]


async def get_identity(identity: SessionIdentityDependency) -> AuthContext:
    """统一在业务入口拦截尚未设置个人密码的账号。"""
    if identity.must_change_password:
        raise ApplicationError("PASSWORD_CHANGE_REQUIRED", "请先修改初始密码", 403)
    return identity


IdentityDependency = Annotated[AuthContext, Depends(get_identity)]


async def get_system_admin(request: Request, identity: IdentityDependency) -> AuthContext:
    """校验部署级权限，与当前工作空间角色无关。"""
    if not auth_service(request).is_system_admin(identity):
        raise ApplicationError("SYSTEM_ADMIN_REQUIRED", "只有系统管理员可以管理部署配置", 403)
    return identity


SystemAdminDependency = Annotated[AuthContext, Depends(get_system_admin)]


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
    scope = await auth_service(request).scope(identity, selected_id, write)
    # 只有通过成员校验的空间才能归档 HTTP 诊断。
    request.state.diagnostic_workspace_id = scope.workspace_id
    return scope


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
async def current_user(request: Request, identity: SessionIdentityDependency, response: Response) -> SessionView:
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


@router.post("/auth/change-password", status_code=204)
async def change_password(body: PasswordChange, request: Request, identity: SessionIdentityDependency) -> Response:
    """让当前用户改密并清除当前浏览器已失效的 Cookie。"""
    await auth_service(request).change_password(identity, body)
    return clear_session_cookie()


def clear_session_cookie() -> Response:
    """沿用会话 Cookie 的作用域，兼容所有登录部署配置。"""
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/api", httponly=True, samesite="lax")
    return response


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
    return clear_session_cookie()


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
