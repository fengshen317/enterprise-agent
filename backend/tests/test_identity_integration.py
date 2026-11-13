"""真实 PostgreSQL 身份模型约束与无损增量迁移验证。"""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.models.conversation import Conversation, Message, Run
from app.models.identity import User, Workspace, WorkspaceMembership
from app.models.system import SystemMetadata

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]
# 仅验证哈希字段形状，不能用于登录；真实哈希生成和校验在 3.2 实现。
HASH_SHAPE = "$argon2id$v=19$m=65536,t=3,p=4$test-salt$test-digest"


async def test_identity_constraints_and_migration_preservation() -> None:
    """验证身份约束、成员多对多关系，并保留迁移前问答和摘要。"""
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and target.database.endswith("_test")
    environment = {**os.environ, "EA_DATABASE_NAME": target.database}

    async def migrate(*arguments: str) -> None:
        """只对已确认的专用测试库执行迁移。

        Args:
            arguments: Alembic 参数。
        """
        await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "alembic", *arguments], cwd=BACKEND_DIR, env=environment, check=True
        )

    await migrate("upgrade", "head")
    engine = create_async_engine(target)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                alice, bob, first, second = (uuid4() for _ in range(4))
                await connection.execute(
                    insert(User),
                    [
                        {"id": alice, "username": "qa_alice", "display_name": "测试甲", "password_hash": HASH_SHAPE},
                        {"id": bob, "username": "qa_bob", "display_name": "测试乙", "password_hash": HASH_SHAPE},
                    ],
                )
                await connection.execute(
                    insert(Workspace),
                    [
                        {"id": first, "slug": "qa-first", "name": "空间甲", "created_by_user_id": alice},
                        {"id": second, "slug": "qa-second", "name": "空间乙", "created_by_user_id": bob},
                    ],
                )
                await connection.execute(
                    insert(WorkspaceMembership),
                    [
                        {"workspace_id": first, "user_id": alice, "role": "admin"},
                        {"workspace_id": second, "user_id": alice, "role": "member"},
                        {"workspace_id": first, "user_id": bob, "role": "member"},
                    ],
                )
                rejected = [
                    (
                        insert(User).values(username="qa_alice", display_name="重复", password_hash=HASH_SHAPE),
                        "uq_users_username",
                    ),
                    (update(User).where(User.id == alice).values(username="QA_ALICE"), "ck_users_username_format"),
                    (update(User).where(User.id == alice).values(username=" qa_alice "), "ck_users_username_format"),
                    (update(User).where(User.id == alice).values(username="甲用户"), "ck_users_username_format"),
                    (update(User).where(User.id == alice).values(username="ab"), "ck_users_username_format"),
                    (
                        update(User).where(User.id == alice).values(display_name=" \t\n"),
                        "ck_users_display_name_nonempty",
                    ),
                    (
                        update(User).where(User.id == alice).values(password_hash="plain-password"),
                        "ck_users_password_hash_format",
                    ),
                    (update(User).where(User.id == alice).values(status="unknown"), "ck_users_status"),
                    (
                        update(User).where(User.id == alice).values(session_version=0),
                        "ck_users_session_version_positive",
                    ),
                    (update(Workspace).where(Workspace.id == second).values(slug="qa-first"), "uq_workspaces_slug"),
                    (
                        update(Workspace).where(Workspace.id == first).values(slug="UPPER-SLUG"),
                        "ck_workspaces_slug_format",
                    ),
                    (update(Workspace).where(Workspace.id == first).values(slug="bad-"), "ck_workspaces_slug_format"),
                    (update(Workspace).where(Workspace.id == first).values(name=" \t"), "ck_workspaces_name_nonempty"),
                    (update(Workspace).where(Workspace.id == first).values(status="unknown"), "ck_workspaces_status"),
                    (
                        update(Workspace).where(Workspace.id == first).values(created_by_user_id=uuid4()),
                        "fk_workspaces_created_by_user_id_users",
                    ),
                    (insert(WorkspaceMembership).values(workspace_id=first, user_id=alice), "pk_workspace_memberships"),
                    (
                        insert(WorkspaceMembership).values(workspace_id=first, user_id=uuid4()),
                        "fk_workspace_memberships_user_id_users",
                    ),
                    (
                        insert(WorkspaceMembership).values(workspace_id=uuid4(), user_id=alice),
                        "fk_workspace_memberships_workspace_id_workspaces",
                    ),
                    (
                        update(WorkspaceMembership).where(WorkspaceMembership.user_id == bob).values(role="owner"),
                        "ck_workspace_memberships_role",
                    ),
                    (
                        update(WorkspaceMembership).where(WorkspaceMembership.user_id == bob).values(status="unknown"),
                        "ck_workspace_memberships_status",
                    ),
                    (
                        delete(Workspace).where(Workspace.id == first),
                        "fk_workspace_memberships_workspace_id_workspaces",
                    ),
                    (delete(User).where(User.id == bob), "fk_workspaces_created_by_user_id_users"),
                ]
                for statement, expected in rejected:
                    async with connection.begin_nested() as savepoint:
                        with pytest.raises(IntegrityError) as failure:
                            await connection.execute(statement)
                        assert expected in str(failure.value.orig)
                        await savepoint.rollback()
                roles = (
                    (
                        await connection.execute(
                            select(WorkspaceMembership.role)
                            .where(WorkspaceMembership.user_id == alice)
                            .order_by(WorkspaceMembership.workspace_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert sorted(roles) == ["admin", "member"]
                await connection.execute(
                    update(WorkspaceMembership)
                    .where(WorkspaceMembership.workspace_id == first, WorkspaceMembership.user_id == bob)
                    .values(status="revoked")
                )
                assert (
                    await connection.scalar(
                        select(WorkspaceMembership.status).where(
                            WorkspaceMembership.workspace_id == first, WorkspaceMembership.user_id == bob
                        )
                    )
                    == "revoked"
                )
                assert await connection.scalar(select(User.session_version).where(User.id == alice)) == 1
                assert await connection.scalar(select(Workspace.status).where(Workspace.id == first)) == "active"
            finally:
                await transaction.rollback()
        conversation_id, run_id = uuid4(), uuid4()
        now = datetime.now(UTC)
        async with engine.begin() as connection:
            await connection.execute(
                insert(Conversation).values(
                    id=conversation_id,
                    title="迁移保留身份基线",
                    summary="迁移前已有摘要",
                    summary_through_sequence=2,
                    summary_version=1,
                    summary_model="test-model",
                    summary_updated_at=now,
                    next_message_sequence=3,
                )
            )
            await connection.execute(
                insert(Run).values(
                    id=run_id,
                    conversation_id=conversation_id,
                    idempotency_key="retained",
                    model="test-model",
                    input_sequence=1,
                    status="completed",
                    finished_at=now,
                )
            )
            await connection.execute(
                insert(Message),
                [
                    {
                        "conversation_id": conversation_id,
                        "sequence": 1,
                        "role": "user",
                        "content": "旧问题",
                        "status": "completed",
                        "run_id": None,
                    },
                    {
                        "conversation_id": conversation_id,
                        "sequence": 2,
                        "role": "assistant",
                        "content": "旧回答",
                        "status": "completed",
                        "run_id": run_id,
                    },
                ],
            )
        await migrate("downgrade", "0003")
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert not {"users", "workspaces", "workspace_memberships"}.intersection(names)
            assert (
                await connection.scalar(select(SystemMetadata.value).where(SystemMetadata.key == "schema_version"))
                == "3"
            )
        await migrate("upgrade", "head")
        await migrate("check")
        async with engine.begin() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert {"users", "workspaces", "workspace_memberships"}.issubset(names)
            row = (
                await connection.execute(
                    select(
                        Conversation.summary, Conversation.summary_through_sequence, Conversation.summary_version
                    ).where(Conversation.id == conversation_id)
                )
            ).one()
            assert tuple(row) == ("迁移前已有摘要", 2, 1)
            contents = (
                (
                    await connection.execute(
                        select(Message.content)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.sequence)
                    )
                )
                .scalars()
                .all()
            )
            assert contents == ["旧问题", "旧回答"]
            assert await connection.scalar(select(Run.status).where(Run.id == run_id)) == "completed"
            await connection.execute(delete(Message).where(Message.conversation_id == conversation_id))
            await connection.execute(delete(Run).where(Run.id == run_id))
            await connection.execute(delete(Conversation).where(Conversation.id == conversation_id))
    finally:
        await engine.dispose()
