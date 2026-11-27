"""连接器管理接口：列表、配置、启停与测试。"""

import asyncio
from time import monotonic
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.admin import _require_admin
from app.api.auth import ScopeDependency
from app.connectors import ima
from app.connectors.base import ConnectorError
from app.connectors.catalog import PERSONAL_CONNECTORS
from app.connectors.registry import ConnectorRegistry
from app.core.errors import ApplicationError
from app.schemas.connectors import (
    ConnectorConfigInput,
    ConnectorDetail,
    ConnectorEnabledInput,
    ConnectorSecretsInput,
    ConnectorTestResult,
    ConnectorView,
    ImaAccountCheckResult,
    ImaProbeResult,
    PersonalAccountInput,
    PersonalAccountView,
    YangJiBaoPortfolioView,
)
from app.services.auth import require_membership
from app.services.connector_service import ConnectorService
from app.services.personal_credentials import account_status, read_credentials, remove_credentials, save_credentials
from app.services.system_logs import emit_system_log

router = APIRouter(prefix="/api", tags=["连接器管理"])
_IMA_PROBE_TIMEOUT_SECONDS = 15
# 使用固定的高熵探测词，避免按成员输入的内容搜索个人笔记。
_IMA_NOTE_PROBE_QUERY = "7cfd2c0390994d1293a8822b63fecb28"
_IMA_PUBLIC_ERRORS = frozenset({
    "auth_failed", "invalid_arguments", "invalid_output", "network_error", "permission_denied", "timeout",
    "upstream_error",
})


def service(request: Request) -> ConnectorService:
    """获取连接器服务。

    Args:
        request: 当前应用请求。

    Returns:
        使用应用连接器注册表的服务。
    """
    return ConnectorService(
        request.app.state.database.session_factory,
        request.app.state.connector_registry,
    )


@router.get("/connectors", response_model=list[ConnectorView])
async def list_connectors(request: Request, response: Response, scope: ScopeDependency) -> list[ConnectorView]:
    """列出当前可见的连接器。

    Args:
        request: 应用请求。
        response: 禁止缓存。
        scope: 可信身份。

    Returns:
        连接器列表。
    """
    response.headers["Cache-Control"] = "no-store"
    return await service(request).list_connectors()


@router.get("/connectors-builtin", response_model=list[dict[str, Any]])
async def list_builtin_connectors(request: Request, response: Response, scope: ScopeDependency) -> list[dict[str, Any]]:
    """列出内置连接器模板（含已安装状态）。

    Args:
        request: 应用请求。
        response: 禁止缓存。
        scope: 可信身份。

    Returns:
        内置模板列表。
    """
    response.headers["Cache-Control"] = "no-store"
    return await service(request).list_builtins()


@router.post("/admin/connectors/{key}/install", response_model=ConnectorDetail)
async def install_builtin(key: str, request: Request, scope: ScopeDependency) -> ConnectorDetail:
    """从内置模板安装连接器（管理员）。

    Args:
        key: 内置连接器标识。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        安装后的连接器详情。
    """
    await _require_admin(request, scope)
    return await service(request).install_builtin(key)


@router.get("/connectors/{key}", response_model=ConnectorDetail)
async def get_connector(key: str, request: Request, response: Response, scope: ScopeDependency) -> ConnectorDetail:
    """读取连接器详情（含工具和配置值）。

    Args:
        key: 连接器标识。
        request: 应用请求。
        response: 禁止缓存包含当前账号权限的详情。
        scope: 可信身份。

    Returns:
        连接器详情。
    """
    response.headers["Cache-Control"] = "no-store"
    detail = await service(request).get_detail(key, user_id=scope.user_id)
    if key in PERSONAL_CONNECTORS:
        async with request.app.state.database.session_factory() as session:
            detail.personal_account = await account_status(session, scope, key)
        detail.can_view_holdings = key == "yangjibao" and detail.enabled and detail.personal_account.bound
    return detail


@router.put("/connectors/{key}/my-account", response_model=PersonalAccountView)
async def bind_personal_account(
    key: str, body: PersonalAccountInput, request: Request, response: Response, scope: ScopeDependency
) -> PersonalAccountView:
    """当前登录成员替换本人凭证，响应中只包含绑定状态。"""
    if key not in PERSONAL_CONNECTORS:
        raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不支持个人绑定", 404)
    if key in {"tmeet", "yangjibao"}:
        raise ApplicationError("CONNECTOR_AUTH_REQUIRED", "请通过个人账号扫码授权", 409)
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.database.session_factory.begin() as session:
        await save_credentials(session, request.app.state.settings, scope, key, body.credentials)
        return await account_status(session, scope, key)


@router.delete("/connectors/{key}/my-account", response_model=PersonalAccountView)
async def unbind_personal_account(
    key: str, request: Request, response: Response, scope: ScopeDependency
) -> PersonalAccountView:
    """按当前身份幂等解绑，即使连接器已停用也能删除凭证。"""
    if key not in PERSONAL_CONNECTORS:
        raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不支持个人绑定", 404)
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.database.session_factory.begin() as session:
        await request.app.state.connector_registry.personal_auth.cancel(key, scope)
        await remove_credentials(session, scope, key)
        return await account_status(session, scope, key)


async def _probe_ima(
    name: str, arguments: dict[str, object], credentials: dict[str, str], workspace_id: UUID
) -> ImaProbeResult:
    """执行一个限时只读探针，只归档公开检查状态。

    Args:
        name: 预先选定的 ima 只读工具名。
        arguments: 固定的探测参数。
        credentials: 已验证成员的当次凭证。
        workspace_id: 已验证工作空间。

    Returns:
        不含上游结果的检查状态与诊断编号。
    """
    started = monotonic()
    error_code = None
    details: dict[str, str | int] = {}
    try:
        async with asyncio.timeout(_IMA_PROBE_TIMEOUT_SECONDS):
            # 成功返回零条结果也证明上游接受了本次请求。
            await ima.invoke(name, arguments, credentials)
    except ConnectorError as exc:
        error_code = exc.code if exc.code in _IMA_PUBLIC_ERRORS else "check_failed"
        details = exc.diagnostics
    except TimeoutError:
        error_code = "timeout"
        details = {"phase": "transport"}
    except Exception:
        error_code = "check_failed"
    status: Literal["success", "error"] = "error" if error_code else "success"
    diagnostic_id = emit_system_log(
        component="connector",
        event="connector_check",
        outcome=status,
        workspace_id=workspace_id,
        connector_key="ima",
        tool_name=name,
        error_code=error_code.upper() if error_code else None,
        duration_ms=int((monotonic() - started) * 1000),
        details=details,
    )
    return ImaProbeResult(status=status, error_code=error_code, diagnostic_id=diagnostic_id)


@router.post("/connectors/ima/my-account/check", response_model=ImaAccountCheckResult)
async def check_ima_account(request: Request, response: Response, scope: ScopeDependency) -> ImaAccountCheckResult:
    """用当前成员已保存的凭证分别检查 ima 知识库和笔记。

    Args:
        request: 应用请求。
        response: 禁止缓存账号检查结果。
        scope: 当前可信用户和工作空间。

    Returns:
        两项互相独立的检查结果。
    """
    response.headers["Cache-Control"] = "no-store"
    async with request.app.state.database.session_factory() as session:
        await require_membership(session, scope)
        credentials = await read_credentials(session, request.app.state.settings, scope, "ima")
    knowledge_base, notes = await asyncio.gather(
        _probe_ima("search_knowledge_base", {"limit": 1}, credentials, scope.workspace_id),
        _probe_ima("search_note", {"query": _IMA_NOTE_PROBE_QUERY, "limit": 1}, credentials, scope.workspace_id),
    )
    return ImaAccountCheckResult(knowledge_base=knowledge_base, notes=notes)


@router.get("/connectors/yangjibao/holdings", response_model=YangJiBaoPortfolioView)
async def get_yangjibao_holdings(
    request: Request, response: Response, scope: ScopeDependency
) -> YangJiBaoPortfolioView:
    """获取绑定账号的持仓金额与行情，禁止缓存该响应。"""
    response.headers["Cache-Control"] = "no-store"
    return await service(request).get_yangjibao_holdings(scope)


class ConnectorUpdateInput(ConnectorConfigInput, ConnectorSecretsInput):
    """连接器配置更新（含非密钥和密钥字段）。"""

    pass


@router.put("/admin/connectors/{key}/config", response_model=ConnectorDetail)
async def update_config(
    key: str, body: ConnectorUpdateInput, request: Request, scope: ScopeDependency
) -> ConnectorDetail:
    """更新连接器配置（管理员）。

    Args:
        key: 连接器标识。
        body: 配置和密钥。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        更新后的连接器详情。
    """
    await _require_admin(request, scope)
    return await service(request).update_config(key, body.config, body.secrets)


@router.post("/admin/connectors/{key}/enabled", response_model=ConnectorDetail)
async def set_enabled(
    key: str, body: ConnectorEnabledInput, request: Request, scope: ScopeDependency
) -> ConnectorDetail:
    """启停连接器（管理员）。

    Args:
        key: 连接器标识。
        body: 启停状态。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        更新后的连接器详情。
    """
    await _require_admin(request, scope)
    return await service(request).set_enabled(key, body.enabled)


@router.post("/admin/connectors/{key}/test", response_model=ConnectorTestResult)
async def test_connection(key: str, request: Request, scope: ScopeDependency) -> ConnectorTestResult:
    """测试连接器并重新发现工具（管理员）。

    Args:
        key: 连接器标识。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        测试结果。
    """
    await _require_admin(request, scope)
    started = monotonic()
    try:
        result = await service(request).test_connection(key)
    except Exception as exc:
        emit_system_log(
            component="connector",
            event="connector_check",
            outcome="error",
            workspace_id=scope.workspace_id,
            error_code=exc.code if isinstance(exc, ApplicationError) else "CHECK_FAILED",
            duration_ms=int((monotonic() - started) * 1000),
        )
        raise exc

    outcome: Literal["success", "warning", "error"] = "success"
    error_code = result.last_error_code
    if result.status == "disabled":
        outcome, error_code = "warning", "connector_disabled"
    elif result.status != "connected":
        outcome, error_code = "error", error_code or "check_failed"
    elif key in PERSONAL_CONNECTORS:
        outcome, error_code = "warning", "personal_check_required"
    emit_system_log(
        component="connector",
        event="connector_check",
        outcome=outcome,
        workspace_id=scope.workspace_id,
        connector_key=key,
        error_code=error_code.upper() if error_code else None,
        duration_ms=int((monotonic() - started) * 1000),
    )
    return result


@router.post("/admin/connectors/{key}/auth")
@router.post("/connectors/{key}/my-account/auth")
async def authenticate(key: str, request: Request, response: Response, scope: ScopeDependency) -> dict[str, object]:
    """发起连接器授权，返回该服务的安全授权入口。

    Args:
        key: 连接器标识。
        request: 应用请求。
        response: 禁止缓存授权入口。
        scope: 可信身份。

    Returns:
        认证入口和状态。
    """
    response.headers["Cache-Control"] = "no-store"
    return await service(request).authenticate(key, scope)


@router.post("/admin/connectors/yangjibao/auth/status")
@router.post("/connectors/yangjibao/my-account/auth/status")
async def poll_yangjibao_authorization(
    request: Request, response: Response, scope: ScopeDependency
) -> dict[str, object]:
    """轮询养基宝授权，确认后才为当前工作台账号保存凭据。

    Args:
        request: 应用请求。
        response: 禁止缓存扫码数据。
        scope: 当前可信用户和工作空间。

    Returns:
        可向扫码发起人展示的授权进度。
    """
    response.headers["Cache-Control"] = "no-store"
    return await service(request).authenticate("yangjibao", scope, poll=True)


@router.get("/admin/connectors/{key}/auth/status")
@router.get("/connectors/{key}/my-account/auth/status")
async def authentication_status(
    key: str, request: Request, response: Response, scope: ScopeDependency
) -> dict[str, object]:
    """读取管理员发起的授权进度。

    Args:
        key: 连接器标识。
        request: 应用请求。
        response: 禁止缓存授权 URL。
        scope: 可信身份。

    Returns:
        授权进度与可操作提示。
    """
    response.headers["Cache-Control"] = "no-store"
    registry: ConnectorRegistry = request.app.state.connector_registry
    if registry.descriptor(key) is None:
        raise ApplicationError("CONNECTOR_NOT_FOUND", "连接器不存在", 404)
    return registry.personal_auth.status(key, scope)
