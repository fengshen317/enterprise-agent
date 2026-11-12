"""数据库连接与请求级会话管理。"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings


class Database:
    """持有应用级连接池，每次使用创建独立会话。"""

    def __init__(self, settings: Settings) -> None:
        """初始化连接池但不立即连接数据库。

        Args:
            settings: 已校验的应用配置。
        """
        self.engine = create_async_engine(
            settings.sqlalchemy_url,
            pool_pre_ping=True,
            connect_args={"timeout": settings.database_timeout_seconds},
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        """应用退出时释放连接池。"""
        await self.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为单次请求提供会话，退出时释放并回滚未提交事务。

    Args:
        request: 当前请求。

    Yields:
        独立异步数据库会话。
    """
    database: Database = request.app.state.database
    async with database.session_factory() as session:
        yield session


SessionDependency = Annotated[AsyncSession, Depends(get_session)]
