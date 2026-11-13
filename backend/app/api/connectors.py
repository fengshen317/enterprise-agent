"""连接器管理接口：列表、配置、启停与测试。"""

from typing import Any

from fastapi import APIRouter, Request, Response

from app.api.admin import _require_admin
from app.api.auth import ScopeDependency
from app.schemas.connectors import (
    ConnectorConfigInput,
    ConnectorDetail,
    ConnectorEnabledInput,
    ConnectorSecretsInput,
    ConnectorTestResult,
    ConnectorView,
)
from app.services.connector_service import ConnectorService

router = APIRouter(prefix="/api", tags=["连接器管理"])


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
async def list_builtin_connectors(
    request: Request, response: Response, scope: ScopeDependency
) -> list[dict[str, Any]]:
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
async def get_connector(key: str, request: Request, scope: ScopeDependency) -> ConnectorDetail:
    """读取连接器详情（含工具和配置值）。

    Args:
        key: 连接器标识。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        连接器详情。
    """
    return await service(request).get_detail(key)


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
    return await service(request).test_connection(key)


@router.post("/admin/connectors/{key}/auth")
async def authenticate(key: str, request: Request, scope: ScopeDependency) -> dict[str, Any]:
    """触发 CLI 账号认证，返回授权 URL（管理员）。

    Args:
        key: 连接器标识。
        request: 应用请求。
        scope: 可信身份。

    Returns:
        认证信息（auth_url / output）。
    """
    await _require_admin(request, scope)
    return await service(request).authenticate(key)
