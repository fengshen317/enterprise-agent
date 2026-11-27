"""个人扫码会话按插件、空间和用户分别管理，不能互相覆盖授权。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.authorization import CliAuthorization
from app.connectors.base import Connector, ConnectorError
from app.connectors.personal_cli import PersonalCliConnector, meeting_directory, meeting_environment
from app.connectors.yangjibao_authorization import YangJiBaoScan
from app.core.config import Settings
from app.models.connector import Connector as ConnectorRow
from app.schemas.auth import ResourceScope
from app.services.auth import require_membership
from app.services.personal_credentials import save_credentials

ScopeKey = tuple[str, UUID, UUID]


class PersonalAuthorization:
    """短期扫码状态只返回给发起者，确认后才写入加密绑定记录。"""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], settings: Settings, resolve: Callable[[str], Connector | None]
    ) -> None:
        """保存共享平台依赖，不共享成员登录。"""
        self.factory = factory
        self.settings = settings
        self.resolve = resolve
        self.flows: dict[ScopeKey, CliAuthorization | YangJiBaoScan] = {}
        self.locks: dict[ScopeKey, asyncio.Lock] = {}

    async def authorize(self, key: str, scope: ResourceScope, *, poll: bool = False) -> dict[str, object]:
        """同一成员重复点击复用流程，不同成员可以同时扫码。"""
        identity = (key, scope.workspace_id, scope.user_id)
        async with self.locks.setdefault(identity, asyncio.Lock()):
            async with self.factory() as session:
                await require_membership(session, scope, write=True)
                if not await session.scalar(select(ConnectorRow.enabled).where(ConnectorRow.key == key)):
                    raise ConnectorError("not_configured", "请先由管理员安装并启用插件")
            connector = self.resolve(key)
            if not isinstance(connector, PersonalCliConnector):
                raise ConnectorError("not_configured", "请先由管理员安装并启用插件")
            flow = self.flows.get(identity)
            if key == "yangjibao":
                if isinstance(flow, YangJiBaoScan):
                    flow.expire()
                if not poll:
                    if not isinstance(flow, YangJiBaoScan) or flow.state != "awaiting_user":
                        connector._holdings.require_scan_ready()
                        flow = await YangJiBaoScan.start(scope)
                        self.flows[identity] = flow
                if isinstance(flow, YangJiBaoScan):
                    if poll and (token := await flow.poll()) is not None:
                        async with self.factory.begin() as session:
                            await save_credentials(session, self.settings, scope, key, {"token": token})
                        flow.finish("completed")
                    return flow.snapshot()
            elif key == "tmeet":
                if poll:
                    return flow.snapshot() if isinstance(flow, CliAuthorization) else self.empty()
                if isinstance(flow, CliAuthorization) and flow.snapshot()["state"] in {"starting", "awaiting_user"}:
                    return await flow.start()
                if isinstance(flow, CliAuthorization):
                    await flow.close()
                profile = uuid4().hex
                directory = meeting_directory(self.settings, scope, profile)
                env = meeting_environment(directory)

                async def finished() -> None:
                    """仅在同一会话仍有效且 CLI 确认本人授权时保存绑定。"""
                    if self.flows.get(identity) is not current:
                        return
                    await connector.meeting(scope, profile).check_authentication()
                    async with self.factory.begin() as session:
                        if self.flows.get(identity) is current:
                            await save_credentials(
                                session, self.settings, scope, key, {"profile_id": SecretStr(profile)}
                            )

                current = CliAuthorization(
                    str(connector.config.get("cli_path", "tmeet")), finished, env=env, cwd=directory
                )
                self.flows[identity] = current
                return await current.start()
            else:
                raise ConnectorError("not_configured", "该插件请填写个人凭据")
            return self.empty()

    @staticmethod
    def empty() -> dict[str, object]:
        """无当前成员的会话时不回显其他成员的状态。"""
        return {"auth_url": "", "output": "请发起个人账号授权", "already_logged_in": False,
                "state": "failed", "expires_at": None}

    def status(self, key: str, scope: ResourceScope) -> dict[str, object]:
        """纯读取本人会话，不通过 GET 提交扫码确认或返回其他用户的二维码。"""
        flow = self.flows.get((key, scope.workspace_id, scope.user_id))
        return flow.snapshot() if flow is not None else self.empty()

    async def cancel(self, key: str, scope: ResourceScope | None = None) -> None:
        """解绑或管理员停用时先移除会话，再等待退出，防止迟到结果重新绑定。"""
        # 取码还在等待上游时尚未写入 flows，也必须等该成员的锁再取消。
        identities = [(key, scope.workspace_id, scope.user_id)] if scope is not None else [
            identity for identity in self.locks.keys() | self.flows.keys() if identity[0] == key
        ]
        for identity in identities:
            async with self.locks.setdefault(identity, asyncio.Lock()):
                flow = self.flows.pop(identity, None)
                if isinstance(flow, CliAuthorization):
                    await flow.close()
                elif isinstance(flow, YangJiBaoScan):
                    flow.finish("failed")

    async def close(self) -> None:
        """服务关闭时回收全部尚未结束的个人授权进程。"""
        for key in {identity[0] for identity in self.flows}:
            await self.cancel(key)
