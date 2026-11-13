"""健康检查、错误边界和请求关联契约。"""

from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError


async def test_liveness_does_not_query_database(client: AsyncClient, session: AsyncMock) -> None:
    """数据库异常不应影响进程存活判断。

    Args:
        client: 测试客户端。
        session: 会话替身。
    """
    session.scalar.side_effect = OperationalError("", {}, Exception("database secret"))
    response = await client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(response.headers["X-Request-ID"]) == 32
    session.scalar.assert_not_called()


async def test_readiness_requires_schema(client: AsyncClient, session: AsyncMock) -> None:
    """只有迁移标记正确时才报告就绪。

    Args:
        client: 测试客户端。
        session: 会话替身。
    """
    assert (await client.get("/api/health/ready")).json()["status"] == "ready"
    session.scalar.return_value = "7"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = "6"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = "5"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = "4"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = "3"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = "2"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = "1"
    assert (await client.get("/api/health/ready")).status_code == 503
    session.scalar.return_value = None
    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SCHEMA_NOT_READY"


async def test_database_failure_is_sanitized(client: AsyncClient, session: AsyncMock) -> None:
    """连接失败响应不能包含驱动异常与凭证。

    Args:
        client: 测试客户端。
        session: 会话替身。
    """
    session.scalar.side_effect = OperationalError("password=do-not-leak", {}, Exception("secret"))
    response = await client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "do-not-leak" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_timeout_is_unavailable(client: AsyncClient, session: AsyncMock) -> None:
    """超时应返回可诊断的就绪失败。

    Args:
        client: 测试客户端。
        session: 会话替身。
    """
    session.scalar.side_effect = TimeoutError()
    assert (await client.get("/api/health/ready")).status_code == 503


async def test_not_found_has_error_contract(client: AsyncClient) -> None:
    """未知路由同样返回稳定错误结构。

    Args:
        client: 测试客户端。
    """
    response = await client.get("/api/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HTTP_404"


async def test_unexpected_error_is_sanitized(app: FastAPI, client: AsyncClient) -> None:
    """意外异常不暴露内部详情。

    Args:
        app: 测试应用。
        client: 测试客户端。
    """

    @app.get("/test/failure")
    async def failure() -> None:
        """模拟依赖内部故障。"""
        raise RuntimeError("internal-sensitive-detail")

    response = await client.get("/test/failure")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "internal-sensitive-detail" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_validation_does_not_echo_input(app: FastAPI, client: AsyncClient) -> None:
    """参数校验失败不回显原始输入。

    Args:
        app: 测试应用。
        client: 测试客户端。
    """

    @app.get("/test/validate")
    async def validate(request: Request, count: int) -> dict[str, int]:
        """模拟带参数校验的接口。

        Args:
            request: 当前请求。
            count: 整数参数。

        Returns:
            校验后的整数。
        """
        return {"count": count}

    response = await client.get("/test/validate", params={"count": "private-value"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "private-value" not in response.text


async def test_cors_only_allows_configured_origin(client: AsyncClient) -> None:
    """跨域响应仅放行开发源站。

    Args:
        client: 测试客户端。
    """
    allowed = await client.get("/api/health/live", headers={"Origin": "http://localhost:8686"})
    denied = await client.get("/api/health/live", headers={"Origin": "https://untrusted.example"})
    assert allowed.headers["Access-Control-Allow-Origin"] == "http://localhost:8686"
    assert "Access-Control-Allow-Origin" not in denied.headers
