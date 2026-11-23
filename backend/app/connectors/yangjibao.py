"""仅供服务器设定的本人账号读取养基宝持仓基金。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from sys import float_info
from threading import Lock
from typing import TYPE_CHECKING, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, SecretStr

from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorKind,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
    health_exception,
    make_tool_key,
)
from app.connectors.catalog import YANGJIBAO
from app.core.config import BACKEND_DIR
from app.core.errors import ApplicationError
from app.schemas.connectors import YangJiBaoHoldingView, YangJiBaoPortfolioView

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.core.config import Settings

PORTFOLIO_TOOL = "list_holdings"
PORTFOLIO_DETAIL_TOOL = "get_portfolio"
STATUS_TIMEOUT = 5
COMMAND_TIMEOUT = 12
MAX_FUNDS = 50
BINDING_OWNER_KEY = "_scan_owner_user_id"
_PORTFOLIO_LOCK = Lock()
_PROCESS_ENV = ("SystemRoot", "WINDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR")


class PortfolioArgs(BaseModel):
    """不接受账号、文件路径或自由命令，身份由运行上下文决定。"""

    model_config = ConfigDict(strict=True, extra="forbid")


def _finite_number(value: object) -> float | None:
    """清洗 CLI 数字，保留有效的零值。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if -float_info.max <= value <= float_info.max else None
    return None


def _available_number(value: object) -> float | None:
    """按 CLI 的可用标记区分缺失数值和真实零值。"""
    if isinstance(value, dict) and value.get("available") is True:
        return _finite_number(value.get("value"))
    return None


def _quote_date(value: object) -> str | None:
    """保留行情源提供的日期或时间精度。"""
    if isinstance(value, str):
        return value.strip()[:40] or None
    return None


class YangJiBaoConnector:
    """使用固定的 fundpeek，为已绑定的账号隔离持仓数据。"""

    kind: ConnectorKind = "cli"
    key = YANGJIBAO.key

    def __init__(self, settings: Settings, bound_owner: UUID | None = None) -> None:
        """兼容已有的显式账号配置，新绑定使用按用户隔离的目录。"""
        self._owner = settings.yangjibao_owner_user_id or bound_owner
        self._legacy_directory = settings.yangjibao_config_dir if settings.yangjibao_owner_user_id else None
        self._config_root = settings.yangjibao_config_dir or Path.home() / ".enterprise-agent" / "yangjibao"
        executable_name = "fundpeek.exe" if os.name == "nt" else "fundpeek"
        self._executable = settings.yangjibao_cli_path or BACKEND_DIR.parent / ".local" / "bin" / executable_name
        self._closed = False

    @property
    def owner(self) -> UUID | None:
        """绑定归属用于复核扫码和工具调用权限。"""
        return self._owner

    def config_fields(self) -> tuple[ConfigField, ...]:
        """工作台页面不接收凭据路径和账号归属配置。"""
        return ()

    async def connect(self) -> ConnectorStatusInfo:
        """使用本机 CLI 的状态命令检查是否已有授权。"""
        return await self.check_health()

    async def check_health(self) -> ConnectorStatusInfo:
        """健康探测只检查本机凭据，业务同步再验证上游授权。"""
        try:
            if self._owner is None:
                self.require_scan_ready()
                return self._scan_required()
            output = await asyncio.to_thread(self._run, ("status",), STATUS_TIMEOUT)
            if "yangjibao: authenticated" in output.splitlines():
                return ConnectorStatusInfo(status="connected", auth_status="authorized")
            if "yangjibao: not authenticated" in output.splitlines():
                return self._scan_required()
            raise ConnectorError("invalid_output", "无法确认本机养基宝授权状态")
        except ConnectorError as exc:
            if exc.code == "not_configured":
                return ConnectorStatusInfo(status="degraded", error_code=exc.code, message=exc.message)
            return health_exception(exc)

    @staticmethod
    def _scan_required() -> ConnectorStatusInfo:
        """引导用户在工作台打开微信扫码。"""
        return ConnectorStatusInfo(
            status="error", auth_status="required", error_code="not_logged_in", message="请使用微信扫码连接养基宝"
        )

    async def list_tools(self) -> list[ConnectorToolDef]:
        """只有设置了绑定账号才注册私有持仓查询工具。"""
        if self._owner is None:
            return []
        return [
            ConnectorToolDef(
                PORTFOLIO_TOOL,
                "只读同步并列出当前账号的养基宝持仓基金代码和名称；用于接续查询基金行情。",
                PortfolioArgs.model_json_schema(),
                "read",
                "never",
            ),
            ConnectorToolDef(
                PORTFOLIO_DETAIL_TOOL,
                "同步并查询我在养基宝的基金持仓明细，包括持仓金额、份额、成本、市值、涨跌幅、参考单日收益和行情时间。"
                "查询持仓金额或分析个人仓位时使用；空值表示暂无，估算与披露净值有各自的时间。",
                PortfolioArgs.model_json_schema(),
                "read",
                "never",
            ),
        ]

    def tool_specs(self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]) -> list[ToolSpec]:
        """同时约束工具装配和执行，审计仅保留数量。"""
        if self._owner is None:
            return []

        def audit(arguments: BaseModel, result: object) -> dict[str, object]:
            """仅审计同步成功的基金数量。"""
            return {"fund_count": result.get("fund_count")} if isinstance(result, dict) else {}

        def build(definition: ConnectorToolDef) -> ToolSpec:
            """为同一绑定账号构建一种持仓查询。"""
            detail = definition.name == PORTFOLIO_DETAIL_TOOL
            approval = policy[definition.name].approval if definition.name in policy else definition.approval

            async def handler(context: ToolContext, arguments: BaseModel) -> object:
                """从运行身份复核账号，再按工具所需范围同步持仓。"""
                if context.scope.user_id != self._owner:
                    raise ApplicationError("TOOL_NOT_ALLOWED", "当前工具未获授权", 403)
                if detail:
                    portfolio = await self.read_portfolio(context.scope.user_id)
                    return {"source": "养基宝", **portfolio.model_dump(mode="json")}
                return await asyncio.to_thread(self._read_holdings)

            return ToolSpec(
                key=make_tool_key(self.key, definition.name),
                version=1,
                display_name="我的养基宝持仓明细" if detail else "我的养基宝持仓",
                description=definition.description,
                args_model=PortfolioArgs,
                risk="read",
                approval=approval,
                timeout_seconds=2 * COMMAND_TIMEOUT + STATUS_TIMEOUT,
                quota_per_run=1,
                handler=handler,
                audit=audit,
                child_allowed=False,
                source="connector",
                connector_key=self.key,
                owner_user_id=self._owner,
            )

        return [build(definition) for definition in defs]

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        """通用入口没有可信用户身份，私有查询必须走工具执行器。"""
        raise ApplicationError("TOOL_NOT_ALLOWED", "当前工具未获授权", 403)

    async def read_portfolio(self, user_id: UUID) -> YangJiBaoPortfolioView:
        """仅为绑定账号同步持仓明细，供页面或问答工具使用。"""
        if user_id != self._owner:
            raise ApplicationError("YANGJIBAO_HOLDINGS_FORBIDDEN", "仅绑定账号可查看养基宝持仓", 403)
        rows = await asyncio.to_thread(self._sync_holdings)
        return YangJiBaoPortfolioView(
            synced_at=datetime.now(UTC),
            fund_count=len(rows),
            funds=[
                YangJiBaoHoldingView(
                    code=code,
                    name=name,
                    holding_amount=_available_number(item.get("holding_amount")),
                    estimated_change_percent=_available_number(item.get("estimated_change_percent")),
                    latest_nav_change_percent=_available_number(item.get("latest_nav_change_percent")),
                    quote_time=_quote_date(item.get("quote_time")),
                    nav_date=_quote_date(item.get("nav_date")),
                    share=_finite_number(item.get("share")),
                    cost_amount=_available_number(item.get("cost_amount")),
                    cost_nav=_available_number(item.get("cost_nav")),
                    estimated_amount=_available_number(item.get("estimated_amount")),
                    latest_nav_amount=_available_number(item.get("latest_nav_amount")),
                    estimated_today_profit_amount=_available_number(item.get("estimated_today_profit_amount")),
                )
                for code, (name, item) in rows.items()
            ],
        )

    def require_scan_ready(self) -> None:
        """扫码前检查主机是否已准备好读取持仓。"""
        exe = self._executable
        if self._closed:
            raise ConnectorError("not_configured", "养基宝持仓连接器已停止")
        if (
            not exe.is_absolute()
            or not exe.is_file()
            or (os.name == "nt" and exe.suffix.lower() != ".exe")
            or not self._config_root.is_absolute()
        ):
            raise ConnectorError("not_configured", "请在后端主机准备 fundpeek 和有效的授权目录")

    def _directory(self, owner: UUID) -> Path:
        """新绑定只使用当前账号的目录，显式配置兼容原有凭据。"""
        return self._legacy_directory or self._config_root / str(owner)

    def save_scan_token(self, owner: UUID, token: SecretStr) -> None:
        """原子更新 fundpeek 凭据文件，避免同步读到半份凭据。"""
        self.require_scan_ready()
        if self._owner is not None and owner != self._owner:
            raise ConnectorError("permission_denied", "养基宝已绑定到其他工作台账号")
        directory = self._directory(owner)
        credentials = directory / "credentials.json"
        pending: Path | None = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            data: object = json.loads(credentials.read_text(encoding="utf-8")) if credentials.is_file() else {}
            if not isinstance(data, dict):
                raise ValueError("凭据文件格式异常")
            payload = cast(dict[str, object], data)
            payload["yangjibao"] = {"token": token.get_secret_value()}
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, prefix=".credentials-", delete=False
            ) as stream:
                pending = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False)
            pending.replace(credentials)
        except (OSError, ValueError):
            raise ConnectorError("permission_denied", "无法保存养基宝授权，请检查后端授权目录") from None
        finally:
            if pending is not None:
                with contextlib.suppress(OSError):
                    pending.unlink(missing_ok=True)

    def _run(self, args: tuple[str, ...], timeout: float) -> str:
        """仅通过可信配置启动固定命令，子进程无需继承应用密钥。"""
        self.require_scan_ready()
        if self._owner is None:
            raise ConnectorError("not_logged_in", "请使用微信扫码连接养基宝")
        directory = self._directory(self._owner)
        if not directory.is_dir():
            raise ConnectorError("not_logged_in", "请使用微信扫码连接养基宝")
        env = {key: os.environ[key] for key in _PROCESS_ENV if key in os.environ}
        env["FUNDPEEK_CONFIG_DIR"] = str(directory)
        try:
            result = subprocess.run(
                [str(self._executable), *args],
                cwd=directory,
                env=env,
                capture_output=True,
                check=False,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            raise ConnectorError("timeout", "养基宝同步响应超时") from None
        except OSError:
            raise ConnectorError("command_failed", "本机 fundpeek 无法启动") from None
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace")
            if any(item in error for item in ("not authenticated", "http 401", "code 401")):
                raise ConnectorError("not_logged_in", "养基宝授权无效，请重新扫码")
            raise ConnectorError("network_error", "养基宝持仓同步暂不可用")
        return result.stdout.decode("utf-8", errors="replace")

    def _sync_holdings(self) -> dict[str, tuple[str, dict[str, object]]]:
        """同步成功后读取当次快照，校验基金代码并按代码去重。"""
        if not _PORTFOLIO_LOCK.acquire(blocking=False):
            raise ConnectorError("timeout", "养基宝持仓正在同步，请稍后重试")
        try:
            self._run(("sync", "yjb"), COMMAND_TIMEOUT)
            raw = self._run(("json",), COMMAND_TIMEOUT)
        finally:
            _PORTFOLIO_LOCK.release()
        if self._closed:
            raise ConnectorError("not_configured", "养基宝持仓连接器已停止")
        try:
            data = json.loads(raw)
        except ValueError:
            raise ConnectorError("invalid_output", "养基宝持仓输出不是有效 JSON") from None
        if not isinstance(data, dict) or not isinstance(rows := data.get("funds"), list):
            raise ConnectorError("invalid_output", "养基宝未返回可识别的持仓列表")
        funds: dict[str, tuple[str, dict[str, object]]] = {}
        for item in rows:
            if not isinstance(item, dict) or not isinstance(code := item.get("code"), str):
                raise ConnectorError("invalid_output", "养基宝持仓基金代码不合法")
            if not re.fullmatch(r"[0-9]{6}", code):
                raise ConnectorError("invalid_output", "养基宝持仓基金代码不合法")
            name = item.get("name")
            funds[code] = (name.strip()[:80] if isinstance(name, str) else "", item)
        return funds

    def _read_holdings(self) -> dict[str, object]:
        """向问答提供匹配基金行情所需的最少字段。"""
        funds = self._sync_holdings()
        return {
            "source": "养基宝",
            "synced_at": datetime.now(UTC).isoformat(),
            "fund_count": len(funds),
            "truncated": len(funds) > MAX_FUNDS,
            "funds": [{"code": code, "name": name} for code, (name, _) in list(funds.items())[:MAX_FUNDS]],
        }

    async def aclose(self) -> None:
        """停用后阻止已有工具快照继续返回持仓。"""
        self._closed = True
