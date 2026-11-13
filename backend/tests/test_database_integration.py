"""在独立 PostgreSQL 测试库中验证迁移与连接。"""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.main import create_app
from app.models.conversation import Conversation, Message, Run

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


async def test_postgres_migration_and_readiness() -> None:
    """验证空库迁移、就绪响应、模型一致性和可丢弃库回退。"""
    settings = Settings()
    if not settings.test_database_name:
        pytest.skip("未配置独立测试数据库 EA_TEST_DATABASE_NAME")
    parsed_url = settings.test_sqlalchemy_url
    database_name = parsed_url.database
    assert database_name and database_name.endswith("_test"), "只允许操作名称以 _test 结尾的专用测试库"
    environment = {
        **os.environ,
        "EA_ENVIRONMENT": "test",
        "EA_DATABASE_HOST": parsed_url.host or "127.0.0.1",
        "EA_DATABASE_PORT": str(parsed_url.port or 5432),
        "EA_DATABASE_USER": parsed_url.username or "postgres",
        "EA_DATABASE_PASSWORD": parsed_url.password or "",
        "EA_DATABASE_NAME": database_name,
    }

    def alembic(*args: str) -> None:
        """在独立进程执行迁移。

        Args:
            args: Alembic 子命令与参数。
        """
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND_DIR, env=environment, check=True)

    await asyncio.to_thread(alembic, "upgrade", "head")
    await asyncio.to_thread(alembic, "check")
    # 在已有消息的场景验证 0003 往返只影响派生摘要，不触碰原始记录。
    retained_id = uuid4()
    retained_engine = create_async_engine(parsed_url)
    try:
        async with retained_engine.begin() as connection:
            await connection.execute(insert(Conversation).values(id=retained_id, title="迁移保留测试"))
            await connection.execute(
                insert(Message).values(
                    conversation_id=retained_id, sequence=1, role="user", status="completed", content="迁移前已有消息"
                )
            )
        await asyncio.to_thread(alembic, "downgrade", "0002")
        async with retained_engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: [item["name"] for item in inspect(sync).get_columns("conversations")]
            )
            assert "summary" not in columns
            assert (
                await connection.scalar(select(Message.content).where(Message.conversation_id == retained_id))
                == "迁移前已有消息"
            )
        await asyncio.to_thread(alembic, "upgrade", "head")
        async with retained_engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        Conversation.summary, Conversation.summary_through_sequence, Conversation.summary_version
                    ).where(Conversation.id == retained_id)
                )
            ).one()
            assert tuple(row) == ("", 0, 0)
            assert (
                await connection.scalar(select(Message.content).where(Message.conversation_id == retained_id))
                == "迁移前已有消息"
            )
    finally:
        await retained_engine.dispose()
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_host=environment["EA_DATABASE_HOST"],
            database_port=int(environment["EA_DATABASE_PORT"]),
            database_user=environment["EA_DATABASE_USER"],
            database_password=environment["EA_DATABASE_PASSWORD"],
            database_name=database_name,
            schedule_enabled=False,
            knowledge_enabled=False,
        )
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/health/ready")).status_code == 200
    constraint_engine = create_async_engine(parsed_url)
    try:
        await verify_conversation_constraints(constraint_engine)
    finally:
        await constraint_engine.dispose()
    # 降到上一版本再升级，验证新迁移可逆且不破坏阶段一的标记。
    await asyncio.to_thread(alembic, "downgrade", "0001")
    previous_engine = create_async_engine(parsed_url)
    try:
        async with previous_engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "conversations" not in names
            assert "system_metadata" in names
    finally:
        await previous_engine.dispose()
    await asyncio.to_thread(alembic, "upgrade", "head")
    await asyncio.to_thread(alembic, "check")
    await asyncio.to_thread(alembic, "downgrade", "base")
    engine = create_async_engine(parsed_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "system_metadata" not in names
    finally:
        await engine.dispose()
    await asyncio.to_thread(alembic, "upgrade", "head")


async def verify_conversation_constraints(engine: AsyncEngine) -> None:
    """在事务回滚范围内验证数据库拒绝越界写入。

    Args:
        engine: 仅连接专用测试库的引擎。
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            first, second, run_id = uuid4(), uuid4(), uuid4()
            await connection.execute(
                insert(Conversation), [{"id": first, "title": "约束测试"}, {"id": second, "title": "另一对话"}]
            )
            await connection.execute(
                insert(Run).values(
                    id=run_id, conversation_id=first, idempotency_key="first", model="test-model", input_sequence=1
                )
            )
            await connection.execute(
                insert(Message).values(
                    conversation_id=first, sequence=1, role="user", status="completed", content="原始问题"
                )
            )
            await connection.execute(
                insert(Message).values(
                    conversation_id=first,
                    run_id=run_id,
                    sequence=2,
                    role="assistant",
                    status="streaming",
                    content="部分内容",
                )
            )
            rejected = [
                # 同一对话只能存在一个未结束运行。
                insert(Run).values(conversation_id=first, idempotency_key="another", model="test", input_sequence=3),
                insert(Message).values(conversation_id=first, sequence=1, role="user", status="completed"),
                insert(Message).values(
                    conversation_id=second, run_id=run_id, sequence=1, role="assistant", status="streaming"
                ),
                insert(Message).values(conversation_id=first, sequence=3, role="assistant", status="completed"),
                insert(Message).values(conversation_id=first, sequence=3, role="user", status="streaming"),
                insert(Message).values(
                    conversation_id=first, run_id=run_id, sequence=3, role="assistant", status="streaming"
                ),
                update(Run).where(Run.id == run_id).values(status="completed"),
                update(Run).where(Run.id == run_id).values(status="invalid"),
                update(Conversation).where(Conversation.id == first).values(next_message_sequence=0),
                update(Conversation).where(Conversation.id == first).values(summary_through_sequence=1),
                update(Conversation).where(Conversation.id == first).values(summary="缺少覆盖范围"),
            ]
            for statement in rejected:
                async with connection.begin_nested() as savepoint:
                    with pytest.raises(IntegrityError):
                        await connection.execute(statement)
                    await savepoint.rollback()
            await connection.execute(
                update(Run).where(Run.id == run_id).values(status="completed", finished_at=datetime.now(UTC))
            )
            # 已终止运行释放并发位置，但仍保留幂等键，不允许重建同一次尝试。
            async with connection.begin_nested() as savepoint:
                with pytest.raises(IntegrityError):
                    await connection.execute(
                        insert(Run).values(
                            conversation_id=first, idempotency_key="first", model="test", input_sequence=3
                        )
                    )
                await savepoint.rollback()
            await connection.execute(
                insert(Run).values(conversation_id=first, idempotency_key="retry", model="test", input_sequence=3)
            )
            contents = (
                (
                    await connection.execute(
                        select(Message.content).where(Message.conversation_id == first).order_by(Message.sequence)
                    )
                )
                .scalars()
                .all()
            )
            assert contents == ["原始问题", "部分内容"]
        finally:
            await transaction.rollback()
