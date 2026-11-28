"""在独立 PostgreSQL schema 验证预算审计迁移的历史兼容。"""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

pytestmark = pytest.mark.integration
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0031_tool_call_budgets.py"


async def test_budget_migration_preserves_old_audits_and_downgrades() -> None:
    """历史系统动作、拒绝记录正确分类，回退不删除原执行记录。"""
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and target.database.endswith("_test")
    engine = create_async_engine(target)
    schema = "budget_probe_" + uuid4().hex
    spec = importlib.util.spec_from_file_location("budget_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def upgrade(connection: Connection) -> None:
        """在已限定的测试 schema 中运行真实 Alembic 操作。"""
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    def downgrade(connection: Connection) -> None:
        """验证降级只移除本次新加的字段。"""
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE system_metadata (key text PRIMARY KEY, value text NOT NULL)"))
            await connection.execute(text("INSERT INTO system_metadata VALUES ('schema_version', '30')"))
            await connection.execute(text("CREATE TABLE tool_calls (tool_name text NOT NULL, error_code text)"))
            await connection.execute(text(
                "INSERT INTO tool_calls VALUES ('qq_mail__search_messages', NULL), "
                "('update_plan', NULL), ('model_request', 'MODEL_CALL_LIMIT'), "
                "('qq_mail__read_message', 'TOOL_CALL_LIMIT')"
            ))
            await connection.run_sync(upgrade)
            rows = (await connection.execute(text(
                "SELECT tool_name, is_system, budget_reserved FROM tool_calls ORDER BY tool_name"
            ))).all()
            assert rows == [
                ("model_request", True, False), ("qq_mail__read_message", False, False),
                ("qq_mail__search_messages", False, True), ("update_plan", True, True),
            ]
            assert await connection.scalar(text("SELECT value FROM system_metadata")) == "31"
            await connection.run_sync(downgrade)
            assert await connection.scalar(text("SELECT count(*) FROM tool_calls")) == 4
            assert await connection.scalar(text("SELECT value FROM system_metadata")) == "30"
            await connection.run_sync(upgrade)
            assert await connection.scalar(text("SELECT count(*) FROM tool_calls")) == 4
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
