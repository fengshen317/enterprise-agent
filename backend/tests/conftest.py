"""不依赖真实数据库的 HTTP 测试夹具。"""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.main import create_app


@pytest.fixture
def app() -> FastAPI:
    """构建独立测试应用。

    Returns:
        不读取开发环境文件的应用。
    """
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            task_scheduler_enabled=False,
            schedule_enabled=False,
            knowledge_enabled=False,
        )
    )


@pytest.fixture
def session(app: FastAPI) -> AsyncMock:
    """替换请求级会话以验证连接异常契约。

    Args:
        app: 测试应用。

    Returns:
        可控制查询结果的会话替身。
    """
    mock = AsyncMock(spec=AsyncSession)
    mock.scalar.return_value = "16"

    async def override() -> AsyncIterator[AsyncSession]:
        """提供隔离会话。

        Yields:
            测试会话。
        """
        yield mock

    app.dependency_overrides[get_session] = override
    return mock


@pytest.fixture
async def client(app: FastAPI, session: AsyncMock) -> AsyncIterator[AsyncClient]:
    """运行完整应用生命周期，验证连接池清理路径。

    Args:
        app: 测试应用。
        session: 已注入的会话替身。

    Yields:
        异步 HTTP 客户端。
    """
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as http:
            yield http
