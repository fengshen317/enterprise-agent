"""把扫码 CLI 的登录和工具调用绑定到可信用户与工作空间。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from pydantic import BaseModel, SecretStr

from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorKind,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
)
from app.connectors.cli import CliConnector, CliRunner
from app.connectors.yangjibao import PORTFOLIO_DETAIL_TOOL, YangJiBaoConnector
from app.core.config import Settings
from app.schemas.auth import ResourceScope
from app.services.personal_credentials import read_credentials

_ENV_KEYS = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")


async def finish_thread[Result](operation: Callable[[], Result]) -> Result:
    """取消请求时仍等待 CLI 线程退出，之后才能销毁含凭据的临时目录。"""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def meeting_directory(settings: Settings, scope: ResourceScope, profile_id: str) -> Path:
    """目录名仅接受内部生成的 UUID，避免路径或账号由模型参数指定。"""
    profile = UUID(profile_id)
    return settings.personal_connector_data_dir / str(scope.workspace_id) / str(scope.user_id) / "tmeet" / profile.hex


def meeting_environment(directory: Path) -> dict[str, str]:
    """同时隔离官方配置目录和加密数据目录，禁止继承共享登录状态。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env.update({
        "TMEET_CLI_CONFIG_DIR": str(directory / "config"),
        "TMEET_CLI_DATA_DIR": str(directory / "data"),
        "HOME": str(directory), "USERPROFILE": str(directory),
        "APPDATA": str(directory / "appdata"), "LOCALAPPDATA": str(directory / "localdata"),
    })
    return env


@asynccontextmanager
async def personal_holdings(
    settings: Settings, scope: ResourceScope, credentials: dict[str, str]
) -> AsyncIterator[YangJiBaoConnector]:
    """只在一次调用期间将解密后的 token 交给独立的 fundpeek 临时目录。"""
    with TemporaryDirectory(prefix="ea-holdings-") as root:
        local = settings.model_copy(update={"yangjibao_owner_user_id": None, "yangjibao_config_dir": Path(root)})
        connector = YangJiBaoConnector(local, scope.user_id)
        await finish_thread(lambda: connector.save_scan_token(scope.user_id, SecretStr(credentials["token"])))
        try:
            yield connector
        finally:
            await connector.aclose()


class PersonalCliConnector:
    """共享工具定义，每次执行重新读取本人的绑定与授权目录。"""

    kind: ConnectorKind = "cli"

    def __init__(self, key: str, config: dict[str, object], settings: Settings) -> None:
        """不捕获任何成员凭据，也不读取服务器默认登录。"""
        self.key = key
        self.config = config
        self.settings = settings
        self._template = CliConnector(key=key, config=config, settings=settings)
        self._holdings = YangJiBaoConnector(
            settings.model_copy(update={"yangjibao_owner_user_id": None}), UUID(int=0)
        )

    def config_fields(self) -> tuple[ConfigField, ...]:
        """成员授权入口不接受平台共享凭据。"""
        return ()

    async def connect(self) -> ConnectorStatusInfo:
        """平台状态只代表工具已安装，个人状态由成员绑定单独给出。"""
        return await self.check_health()

    async def check_health(self) -> ConnectorStatusInfo:
        """检查可执行程序，不使用任何已登录的账号。"""
        try:
            if self.key == "yangjibao":
                self._holdings.require_scan_ready()
            else:
                await CliRunner().run([str(self.config.get("cli_path", "tmeet")), "--version"], 5)
            return ConnectorStatusInfo(status="connected", auth_status="unknown")
        except ConnectorError:
            return ConnectorStatusInfo(
                status="error", auth_status="unknown", error_code="not_configured",
                message="插件运行组件尚未安装，请管理员完成部署后再绑定个人账号",
            )

    async def list_tools(self) -> list[ConnectorToolDef]:
        """工具目录不依赖某一成员是否已扫码。"""
        return await (self._holdings.list_tools() if self.key == "yangjibao" else self._template.list_tools())

    def tool_specs(self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]) -> list[ToolSpec]:
        """保留原有风险与审批规则，替换为逐次复核身份的执行入口。"""
        source = self._holdings if self.key == "yangjibao" else self._template
        specs = source.tool_specs(defs, policy)

        def scoped(spec: ToolSpec, name: str) -> ToolSpec:
            """闭包仅保留动作名，凭证不能进入注册表或运行快照。"""
            async def handler(context: ToolContext, arguments: BaseModel) -> object:
                """即时复核绑定，解绑后的历史运行也不能继续访问账号。"""
                async with context.factory() as session:
                    credentials = await read_credentials(session, context.settings, context.scope, self.key)
                if self.key == "yangjibao":
                    async with personal_holdings(context.settings, context.scope, credentials) as connector:
                        if name == PORTFOLIO_DETAIL_TOOL:
                            data = await connector.read_portfolio(context.scope.user_id)
                            return {"source": "养基宝", **data.model_dump(mode="json")}
                        return await finish_thread(connector._read_holdings)
                meeting = self.meeting(context.scope, credentials["profile_id"])
                return await meeting.call(name, arguments.model_dump(exclude_none=True))

            return replace(spec, handler=handler, owner_user_id=None)

        return [scoped(spec, definition.name) for spec, definition in zip(specs, defs, strict=True)]

    def meeting(self, scope: ResourceScope, profile_id: str) -> CliConnector:
        """登录和调用复用同一组隔离目录。"""
        directory = meeting_directory(self.settings, scope, profile_id)
        return CliConnector(
            key="tmeet", config=self.config, settings=self.settings,
            runner=CliRunner(env=meeting_environment(directory), cwd=directory),
        )

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        """拒绝没有可信身份的直接调用。"""
        raise ConnectorError("permission_denied", "个人工具需要当前工作空间用户身份")

    async def aclose(self) -> None:
        """授权进程由注册表的个人会话管理器回收。"""
        await self._holdings.aclose()
