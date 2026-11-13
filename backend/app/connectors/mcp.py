"""MCP 连接器：连接 MCP 服务器，自动发现并暴露工具。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.capabilities.spec import ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)


class McpConnector:
    """MCP 连接器：通过 MCP 协议发现并调用外部工具。

    支持 stdio（子进程）和 HTTP 传输。
    """

    def __init__(self, *, key: str, config: dict[str, object], settings: Settings) -> None:
        """初始化连接器。

        Args:
            key: 连接器标识。
            config: 非密钥配置（transport/command/args/url 等）。
            settings: 应用配置。
        """
        self.key = key
        self.kind = "mcp"
        self._config = config
        self._settings = settings
        self._session: Any = None
        self._streams: Any = None

    def config_fields(self) -> tuple[ConfigField, ...]:
        """声明配置项。

        Returns:
            MCP 配置字段。
        """
        return (
            ConfigField(key="transport", label="传输方式", field_type="select", required=True, default="stdio"),
            ConfigField(key="command", label="启动命令（stdio）", field_type="text"),
            ConfigField(key="url", label="服务地址（HTTP）", field_type="text"),
        )

    async def connect(self) -> ConnectorStatusInfo:
        """建立 MCP 连接并发现工具。

        Returns:
            连接状态。
        """
        try:
            await self._ensure_session()
            return ConnectorStatusInfo(status="connected")
        except Exception as exc:
            return ConnectorStatusInfo(status="error", message=str(exc)[:200])

    async def list_tools(self) -> list[ConnectorToolDef]:
        """通过 MCP list_tools 发现工具。

        Returns:
            工具定义列表。
        """
        try:
            await self._ensure_session()
            result = await self._session.list_tools()
            defs: list[ConnectorToolDef] = []
            for tool in result.tools:
                defs.append(
                    ConnectorToolDef(
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.inputSchema if isinstance(tool.inputSchema, dict) else {},
                        risk="external",
                        approval="always",
                    )
                )
            return defs
        except Exception as exc:
            logger.warning("mcp_list_tools_failed", extra={"key": self.key, "error": str(exc)[:100]})
            return []

    def tool_specs(
        self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]
    ) -> list[ToolSpec]:
        """映射 MCP 工具定义为 ToolSpec。

        Args:
            defs: MCP 工具定义。
            policy: 风险/审批覆盖。

        Returns:
            ToolSpec 列表。
        """
        from app.connectors.base import make_tool_key
        from app.connectors.dynamic_args import PassThroughArgs

        specs: list[ToolSpec] = []
        for defn in defs:
            override = policy.get(defn.name)
            risk = override.risk if override else defn.risk
            approval = override.approval if override else defn.approval
            tool_key = make_tool_key(self.key, defn.name)

            async def handler(context: Any, arguments: Any, _name: str = defn.name) -> object:
                """调用 MCP 工具。

                Args:
                    context: 工具执行上下文（连接器不使用）。
                    arguments: 调用参数。
                    _name: 工具原始名。

                Returns:
                    MCP 调用结果。
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
                    args_model=PassThroughArgs,
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
        """执行 MCP 工具调用。

        Args:
            name: 工具原始名。
            arguments: 调用参数。

        Returns:
            MCP 调用结果。
        """
        await self._ensure_session()
        result = await self._session.call_tool(name, arguments)
        # structuredContent 优先，退化 text
        if hasattr(result, "structuredContent") and result.structuredContent:
            return result.structuredContent
        texts = []
        for block in getattr(result, "content", []):
            if hasattr(block, "text"):
                texts.append(block.text)
        return {"text": "\n".join(texts)} if texts else {}

    async def aclose(self) -> None:
        """释放 MCP 会话与传输流。"""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._streams is not None:
            try:
                await self._streams.__aexit__(None, None, None)
            except Exception:
                pass
            self._streams = None

    async def _ensure_session(self) -> None:
        """确保 MCP 会话已建立。

        Raises:
            ConnectorError: MCP SDK 未安装或连接失败。
        """
        if self._session is not None:
            return
        try:
            import mcp  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            logger.warning("mcp_sdk_not_installed")
            return

        transport = str(self._config.get("transport", "stdio"))
        try:
            if transport == "stdio":
                await self._connect_stdio()
            elif transport == "http":
                await self._connect_http()
        except Exception as exc:
            logger.warning("mcp_connect_failed", extra={"key": self.key, "error": str(exc)[:100]})
            raise

    async def _connect_stdio(self) -> None:
        """通过 stdio 子进程连接 MCP 服务器。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client  # type: ignore[import-not-found]

        command = str(self._config.get("command", ""))
        raw_args = self._config.get("args", [])
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []

        params = StdioServerParameters(command=command, args=args)
        # stdio_client 是异步上下文管理器，返回 (read, write) 流
        self._streams = await stdio_client(params).__aenter__()
        read_stream, write_stream = self._streams
        self._session = await ClientSession(read_stream, write_stream).__aenter__()
        await self._session.initialize()

    async def _connect_http(self) -> None:
        """通过 HTTP 连接 MCP 服务器。"""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client  # type: ignore[import-not-found]

        url = str(self._config.get("url", ""))
        self._streams = await streamablehttp_client(url).__aenter__()
        read_stream, write_stream = self._streams[0], self._streams[1]
        self._session = await ClientSession(read_stream, write_stream).__aenter__()
        await self._session.initialize()
