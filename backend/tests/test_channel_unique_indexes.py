"""换应用和重新绑定时，先落库停用/解绑，再插入新行，避免撞上部分唯一索引。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.selectable import Select

from app.channels.crypto import ChannelCrypto
from app.channels.repository import ChannelStore
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.channel import ChannelAccount, ChannelApp, ChannelBindCode
from app.models.identity import User, Workspace

_SCHEMA = """
CREATE TABLE users (id CHAR(32) PRIMARY KEY);
CREATE TABLE workspaces (id CHAR(32) PRIMARY KEY);
CREATE TABLE channel_apps (
    id CHAR(32) PRIMARY KEY,
    workspace_id CHAR(32),
    channel_type VARCHAR(16) NOT NULL,
    external_app_id VARCHAR(128) NOT NULL DEFAULT '',
    credentials_ciphertext TEXT NOT NULL DEFAULT '',
    public_config JSON NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_by_user_id CHAR(32),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_channel_apps_type_active
    ON channel_apps (channel_type) WHERE status = 'active';
CREATE TABLE channel_accounts (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL,
    workspace_id CHAR(32) NOT NULL,
    channel_type VARCHAR(16) NOT NULL DEFAULT 'ilink',
    ilink_bot_id VARCHAR(128) NOT NULL DEFAULT '',
    ilink_user_id VARCHAR(128) NOT NULL DEFAULT '',
    app_id CHAR(32),
    baseurl VARCHAR(300) NOT NULL DEFAULT '',
    bot_token_ciphertext TEXT NOT NULL DEFAULT '',
    updates_cursor TEXT NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    welcome_pending BOOLEAN NOT NULL DEFAULT 0,
    paused_until DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_channel_accounts_user_open
    ON channel_accounts (user_id, channel_type) WHERE status <> 'unbound';
CREATE UNIQUE INDEX uq_channel_accounts_platform_user
    ON channel_accounts (channel_type, ilink_user_id)
    WHERE status <> 'unbound' AND ilink_user_id <> '';
CREATE TABLE channel_bind_codes (
    id CHAR(32) PRIMARY KEY,
    user_id CHAR(32) NOT NULL,
    workspace_id CHAR(32) NOT NULL,
    app_id CHAR(32) NOT NULL,
    code VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture
async def store():
    """在带同样部分唯一索引的库上跑真实 ChannelStore。

    SQLite 不支持 FOR UPDATE，这里只拿掉行锁，索引约束保持不变。

    Yields:
        渠道仓储和会话工厂。
    """
    original = Select.with_for_update
    Select.with_for_update = lambda self, *args, **kwargs: self  # type: ignore[method-assign]
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_foreign_keys(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    async with engine.begin() as connection:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                await connection.exec_driver_sql(statement)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    channel_store = ChannelStore(factory, ChannelCrypto(Settings(_env_file=None)))

    def _restore_timezone(target: ChannelBindCode, _context: object) -> None:
        # SQLite 读回的时间没有时区。生产库是 PostgreSQL，比较前会带 UTC。
        value = target.expires_at
        if value.tzinfo is None:
            target.expires_at = value.replace(tzinfo=UTC)

    event.listen(ChannelBindCode, "load", _restore_timezone)
    try:
        yield channel_store, factory
    finally:
        event.remove(ChannelBindCode, "load", _restore_timezone)
        Select.with_for_update = original  # type: ignore[method-assign]
        await engine.dispose()
        # 引用模型，确保外键能在元数据里找到 users / workspaces。
        assert User.__tablename__ == "users"
        assert Workspace.__tablename__ == "workspaces"


async def test_replacing_app_disables_old_row_before_insert(store) -> None:
    """换一个平台应用时，旧的 active 行先停用并解绑，新行才能插进去。"""
    channel_store, factory = store
    user_id, workspace_id = uuid4(), uuid4()
    first = await channel_store.save_app(
        workspace_id=workspace_id,
        user_id=user_id,
        channel_type="feishu",
        external_app_id="cli_old",
        ciphertext="",
        public_config={},
    )
    async with factory.begin() as session:
        session.add(
            ChannelAccount(
                user_id=user_id,
                workspace_id=workspace_id,
                channel_type="feishu",
                ilink_user_id="ou_old",
                ilink_bot_id="cli_old",
                app_id=first.id,
                status="active",
            )
        )
        session.add(
            ChannelBindCode(
                user_id=user_id,
                workspace_id=workspace_id,
                app_id=first.id,
                code="OLDCODE1",
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    second = await channel_store.save_app(
        workspace_id=workspace_id,
        user_id=user_id,
        channel_type="feishu",
        external_app_id="cli_new",
        ciphertext="",
        public_config={"sandbox": False},
    )
    async with factory() as session:
        apps = list(await session.scalars(select(ChannelApp).order_by(ChannelApp.external_app_id)))
        account = await session.scalar(select(ChannelAccount))
        bind_code = await session.scalar(select(ChannelBindCode))
    assert second.id != first.id
    assert second.external_app_id == "cli_new"
    assert [item.external_app_id for item in apps] == ["cli_new", "cli_old"]
    assert [item.status for item in apps] == ["active", "disabled"]
    assert account is not None and account.status == "unbound" and account.app_id == first.id
    assert bind_code is not None and bind_code.status == "expired"


async def test_rebind_from_disabled_app_releases_unique_rows_first(store) -> None:
    """平台用户和员工还绑在已停用机器人上时，先解绑再插入，两条唯一索引都不冲突。"""
    channel_store, factory = store
    user_id, other_user, workspace_id = uuid4(), uuid4(), uuid4()
    disabled = ChannelApp(
        workspace_id=workspace_id,
        channel_type="dingtalk",
        external_app_id="robot_old",
        credentials_ciphertext="",
        public_config={},
        status="disabled",
    )
    async with factory.begin() as session:
        session.add(disabled)
        await session.flush()
        session.add(
            ChannelAccount(
                user_id=other_user,
                workspace_id=workspace_id,
                channel_type="dingtalk",
                ilink_user_id="staff-1",
                ilink_bot_id="robot_old",
                app_id=disabled.id,
                status="active",
            )
        )
    current = await channel_store.save_app(
        workspace_id=workspace_id,
        user_id=user_id,
        channel_type="dingtalk",
        external_app_id="robot_new",
        ciphertext="",
        public_config={},
    )
    async with factory.begin() as session:
        session.add(
            ChannelBindCode(
                user_id=user_id,
                workspace_id=workspace_id,
                app_id=current.id,
                code="NEWCODE1",
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    bound = await channel_store.redeem_bind_code("dingtalk", "staff-1", "newcode1")
    assert bound is not None
    assert bound.app_id == current.id
    assert bound.user_id == user_id
    assert bound.status == "active"
    async with factory() as session:
        rows = list(await session.scalars(select(ChannelAccount).where(ChannelAccount.channel_type == "dingtalk")))
    assert sorted(item.status for item in rows) == ["active", "unbound"]
    assert sum(item.status == "active" and item.ilink_user_id == "staff-1" for item in rows) == 1


async def test_rebind_same_user_on_disabled_robot_releases_user_index(store) -> None:
    """同一员工还绑在旧机器人上时，先解开 uq_channel_accounts_user_open 再插入。"""
    channel_store, factory = store
    user_id, workspace_id = uuid4(), uuid4()
    disabled = ChannelApp(
        workspace_id=workspace_id,
        channel_type="qq",
        external_app_id="qq_old",
        credentials_ciphertext="",
        public_config={},
        status="disabled",
    )
    async with factory.begin() as session:
        session.add(disabled)
        await session.flush()
        session.add(
            ChannelAccount(
                user_id=user_id,
                workspace_id=workspace_id,
                channel_type="qq",
                ilink_user_id="openid-old",
                ilink_bot_id="qq_old",
                app_id=disabled.id,
                status="active",
            )
        )
    current = await channel_store.save_app(
        workspace_id=workspace_id,
        user_id=user_id,
        channel_type="qq",
        external_app_id="qq_new",
        ciphertext="",
        public_config={},
    )
    async with factory.begin() as session:
        session.add(
            ChannelBindCode(
                user_id=user_id,
                workspace_id=workspace_id,
                app_id=current.id,
                code="QQCODE01",
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    bound = await channel_store.redeem_bind_code("qq", "openid-new", "qqcode01")
    assert bound is not None and bound.ilink_user_id == "openid-new" and bound.app_id == current.id
    async with factory() as session:
        rows = list(await session.scalars(select(ChannelAccount).where(ChannelAccount.channel_type == "qq")))
    assert sorted((item.ilink_user_id, item.status) for item in rows) == [
        ("openid-new", "active"),
        ("openid-old", "unbound"),
    ]


async def test_redeem_still_refuses_to_take_over_the_active_app(store) -> None:
    """同一个正在使用的应用上，不能用绑定码把别人或自己的绑定换掉。"""
    channel_store, factory = store
    owner, intruder, workspace_id = uuid4(), uuid4(), uuid4()
    app = await channel_store.save_app(
        workspace_id=workspace_id,
        user_id=owner,
        channel_type="feishu",
        external_app_id="cli_live",
        ciphertext="",
        public_config={},
    )
    async with factory.begin() as session:
        session.add(
            ChannelAccount(
                user_id=owner,
                workspace_id=workspace_id,
                channel_type="feishu",
                ilink_user_id="ou_owner",
                ilink_bot_id="cli_live",
                app_id=app.id,
                status="active",
            )
        )
        session.add(
            ChannelBindCode(
                user_id=intruder,
                workspace_id=workspace_id,
                app_id=app.id,
                code="TAKEOVER",
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        session.add(
            ChannelBindCode(
                user_id=owner,
                workspace_id=workspace_id,
                app_id=app.id,
                code="SECOND01",
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    with pytest.raises(ApplicationError) as stolen:
        await channel_store.redeem_bind_code("feishu", "ou_owner", "takeover")
    assert stolen.value.code == "CHANNEL_ALREADY_BOUND"
    with pytest.raises(ApplicationError) as again:
        await channel_store.redeem_bind_code("feishu", "ou_other", "second01")
    assert again.value.code == "CHANNEL_ALREADY_BOUND"
