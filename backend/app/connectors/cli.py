"""CLI 连接器：封装命令行工具为 ToolSpec。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.capabilities.spec import ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
    health_failure,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

# 项目根目录（backend/app/connectors/cli.py → parents[3] = 项目根）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PROJECT_CLI_DIRS = [
    _PROJECT_ROOT / "tools" / "node_modules" / ".bin",
    _PROJECT_ROOT / "node_modules" / ".bin",
]


def resolve_cli_path(command: str) -> str:
    """解析 CLI 命令的完整路径。

    优先查找项目内安装的 CLI（tools/node_modules/.bin），
    再查找系统 PATH，最后返回原始命令名。

    Args:
        command: 命令名（如 "tmeet"）。

    Returns:
        可执行文件的完整路径。
    """
    # 1. 查找项目内安装的 CLI（Windows 优先 .cmd）
    for cli_dir in _PROJECT_CLI_DIRS:
        for ext in (".cmd", ".bat", ".exe", ""):
            candidate = cli_dir / f"{command}{ext}"
            if candidate.exists():
                return str(candidate)
    # 2. 查找系统 PATH
    resolved = shutil.which(command)
    return resolved if resolved else command


@dataclass(frozen=True)
class CliResult:
    """CLI 执行结果。"""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        """判断执行是否成功。

        Returns:
            退出码为 0 时返回 True。
        """
        return self.exit_code == 0

    @property
    def json(self) -> Any:
        """解析 stdout 为 JSON。

        Returns:
            JSON 解析结果。

        Raises:
            ConnectorError: stdout 非合法 JSON。
        """
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ConnectorError("invalid_output", f"CLI 输出非合法 JSON: {exc}") from None


class CliRunner:
    """通用 CLI 执行器：子进程管理、超时、错误分类、防注入。"""

    async def run(self, args: list[str], timeout_seconds: float) -> CliResult:
        """执行 CLI 命令。

        用 subprocess.run 在线程池执行，避免 Windows asyncio 子进程兼容问题。

        Args:
            args: 参数数组（不经 shell，防注入）。
            timeout_seconds: 超时秒数。

        Returns:
            CLI 执行结果。

        Raises:
            ConnectorError: 超时或进程启动失败。
        """
        # 优先查找项目内 CLI，再查系统 PATH。
        resolved = resolve_cli_path(args[0])
        exec_args = [resolved] + args[1:]

        def _run_sync() -> tuple[str, str, int]:
            """同步执行 CLI 命令。

            Returns:
                (stdout, stderr, exit_code)。
            """
            proc = subprocess.run(
                exec_args,
                capture_output=True,
                timeout=timeout_seconds,
            )
            stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
            stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
            return stdout, stderr, proc.returncode

        try:
            stdout, stderr, exit_code = await asyncio.get_event_loop().run_in_executor(None, _run_sync)
        except subprocess.TimeoutExpired:
            raise ConnectorError("timeout", f"CLI 执行超时 ({timeout_seconds}s)") from None
        except (OSError, ValueError) as exc:
            raise ConnectorError("command_failed", f"CLI 启动失败: {exc}") from None

        if exit_code != 0:
            code = self._classify_error(exit_code, stderr)
            raise ConnectorError(code, stderr.strip()[:200] or f"CLI 退出码 {exit_code}")

        return CliResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    async def run_json(self, args: list[str], timeout_seconds: float) -> Any:
        """执行并解析 JSON 输出。

        Args:
            args: 参数数组。
            timeout: 超时秒数。

        Returns:
            JSON 解析结果。
        """
        result = await self.run(args, timeout_seconds)
        return result.json

    @staticmethod
    def _classify_error(exit_code: int, stderr: str) -> str:
        """根据退出码和 stderr 分类错误。

        Args:
            exit_code: 进程退出码。
            stderr: 标准错误输出。

        Returns:
            分类后的错误码。
        """
        lower = stderr.lower()
        if any(text in lower for text in ("no such host", "network error", "connection refused", "connection reset")):
            return "network_error"
        if "user config is empty" in lower:
            return "not_logged_in"
        if "permission" in lower or "forbidden" in lower:
            return "permission_denied"
        return "command_failed"


class CliConnector:
    """CLI 连接器：封装命令行工具为 ToolSpec。"""

    def __init__(self, *, key: str, config: dict[str, object], settings: Settings) -> None:
        """初始化连接器。

        Args:
            key: 连接器标识。
            config: 非密钥配置（包含 tools 定义列表）。
            settings: 应用配置。
        """
        self.key = key
        self.kind = "cli"
        self._config = config
        self._settings = settings
        self._runner = CliRunner()

    def config_fields(self) -> tuple[ConfigField, ...]:
        """声明配置项。

        Returns:
            基础配置字段。
        """
        return (ConfigField(key="cli_path", label="CLI 路径", field_type="text", required=True),)

    async def connect(self) -> ConnectorStatusInfo:
        """检查 CLI 可运行性及腾讯会议账号认证状态。

        Returns:
            连接状态。
        """
        cli_path = str(self._config.get("cli_path", "tmeet"))
        try:
            await self._runner.run([cli_path, "--version"], timeout_seconds=5)
            if self.key == "tmeet":
                await self.check_authentication()
            return ConnectorStatusInfo(
                status="connected", auth_status="authorized" if self.key == "tmeet" else "not_required"
            )
        except ConnectorError as exc:
            return health_failure(exc.code)

    async def check_health(self) -> ConnectorStatusInfo:
        """复用无持久连接的 CLI 检查。

        Returns:
            版本和认证检查结果。
        """
        return await self.connect()

    async def check_authentication(self) -> None:
        """检查 tmeet 文本状态；未登录时退出码仍为零，不能只检查进程退出码。

        Raises:
            ConnectorError: 未登录、状态无法识别或命令失败。
        """
        cli_path = str(self._config.get("cli_path", "tmeet"))
        result = await self._runner.run([cli_path, "auth", "status"], timeout_seconds=5)
        if self._runner._classify_error(0, result.stdout + result.stderr) == "network_error":
            raise ConnectorError("network_error", "腾讯会议认证服务连接失败，请检查网络后重试")
        lines = [line.strip().lower() for line in result.stdout.splitlines()]
        if any(line.startswith("not logged in") for line in lines):
            raise ConnectorError("not_logged_in", "腾讯会议未登录，请完成账号授权后重试")
        if "logged in" not in lines:
            raise ConnectorError("invalid_output", "无法识别腾讯会议登录状态，请重新测试连接")

    async def list_tools(self) -> list[ConnectorToolDef]:
        """从配置读取声明式工具定义。

        Returns:
            工具定义列表。
        """
        raw = self._config.get("tools", [])
        if not isinstance(raw, list):
            return []
        defs: list[ConnectorToolDef] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            defs.append(
                ConnectorToolDef(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    input_schema=item.get("input_schema", {}) if isinstance(item.get("input_schema"), dict) else {},
                    risk=item.get("risk", "external"),
                    approval=item.get("approval", "always"),
                )
            )
        return defs

    def tool_specs(self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]) -> list[ToolSpec]:
        """映射工具定义为 ToolSpec。

        Args:
            defs: 工具定义。
            policy: 风险/审批覆盖。

        Returns:
            ToolSpec 列表。
        """
        from app.connectors.base import make_tool_key
        from app.connectors.dynamic_args import make_args_model

        specs: list[ToolSpec] = []
        for defn in defs:
            override = policy.get(defn.name)
            risk = override.risk if override else defn.risk
            approval = override.approval if override else defn.approval
            tool_key = make_tool_key(self.key, defn.name)
            args_model = make_args_model(defn.input_schema, tool_key)

            async def handler(context: Any, arguments: Any, _name: str = defn.name) -> object:
                """调用 CLI 工具。

                Args:
                    context: 工具执行上下文（连接器不使用）。
                    arguments: 调用参数。
                    _name: 工具原始名。

                Returns:
                    CLI 执行结果。
                """
                raw_args = arguments.model_dump() if hasattr(arguments, "model_dump") else {}
                return await self.call(_name, raw_args)

            def audit(arguments: Any, result: object, _name: str = defn.name) -> dict[str, object]:
                """审计只保存工具名和参数摘要。

                Args:
                    arguments: 调用参数。
                    result: 调用结果。
                    _name: 工具原始名。

                Returns:
                    脱敏审计摘要。
                """
                raw_args = arguments.model_dump() if hasattr(arguments, "model_dump") else {}
                return {"tool": _name, "args_keys": list(raw_args.keys())}

            specs.append(
                ToolSpec(
                    key=tool_key,
                    version=1,
                    display_name=defn.name,
                    description=defn.description,
                    args_model=args_model,
                    risk=risk,
                    approval=approval,
                    timeout_seconds=self._settings.tool_timeout_seconds,
                    quota_per_run=5,
                    child_allowed=(risk == "read"),
                    handler=handler,
                    audit=audit,
                    source="connector",
                    connector_key=self.key,
                    input_schema_override=defn.input_schema if defn.input_schema else None,
                )
            )
        return specs

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        """执行一次 CLI 调用。

        Args:
            name: 工具原始名。
            arguments: 调用参数。

        Returns:
            CLI 输出。
        """
        if self.key == "tmeet" and name == "auth_status":
            await self.check_authentication()
            # tmeet auth status 即使指定 JSON 也输出文本；只向模型提供认证状态。
            return {"logged_in": True}
        cli_path = str(self._config.get("cli_path", "tmeet"))
        # 从配置的 tools 列表中找对应命令模板
        raw_tools = self._config.get("tools", [])
        if isinstance(raw_tools, list):
            for item in raw_tools:
                if isinstance(item, dict) and item.get("name") == name:
                    command = item.get("command", [])
                    if isinstance(command, list):
                        args = [cli_path] + self._command_arguments(command, arguments, item.get("input_schema", {}))
                        timeout = float(str(self._config.get("timeout_seconds", "30")))
                        return await self._runner.run_json(args, timeout)
        raise ConnectorError("command_failed", f"工具 {name} 未定义命令")

    @staticmethod
    def _command_arguments(command: list[object], arguments: dict[str, object], schema: object) -> list[str]:
        """省略未提供的可选选项，保留 CLI 的默认查询范围。

        Args:
            command: 命令模板，每个参数占独立元素。
            arguments: 已经通过工具模型校验的参数。
            schema: 用于区分可选参数与必填参数的输入声明。

        Returns:
            不经过 shell 的参数列表。
        """
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        optional = set(properties) - set(required) if isinstance(properties, dict) else set()
        result: list[str] = []
        index = 0
        while index < len(command):
            token = str(command[index])
            if token.startswith("--") and index + 1 < len(command):
                placeholder = re.fullmatch(r"\{(\w+)\}", str(command[index + 1]))
                if placeholder and placeholder[1] in optional and arguments.get(placeholder[1]) is None:
                    # 省略整组 --option {value}，避免把 None 变成实际搜索词或时间。
                    index += 2
                    continue
            result.append(token.format(**arguments))
            index += 1
        return result

    async def aclose(self) -> None:
        """释放资源。"""
