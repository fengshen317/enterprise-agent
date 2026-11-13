"""连接器契约：配置声明、工具发现与统一执行接口。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.capabilities.spec import ToolSpec

ConnectorKind = Literal["cli", "mcp", "api"]
ConnectorStatus = Literal["disabled", "connected", "degraded", "error"]
RiskLevelName = Literal["read", "write", "delete", "external"]
ApprovalModeName = Literal["never", "always"]
ConfigFieldType = Literal["text", "secret", "number", "boolean", "select", "json"]


@dataclass(frozen=True)
class ConfigField:
    """连接器配置表单字段声明，前端动态渲染。"""

    key: str
    label: str
    field_type: ConfigFieldType
    required: bool = False
    default: str = ""
    placeholder: str = ""
    is_secret: bool = False


@dataclass(frozen=True)
class ConnectorToolDef:
    """连接器暴露的工具元数据（发现结果，未绑定 handler）。"""

    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)
    risk: RiskLevelName = "external"
    approval: ApprovalModeName = "always"


@dataclass(frozen=True)
class ConnectorStatusInfo:
    """连接器健康状态。"""

    status: ConnectorStatus
    message: str = ""
    checked_at: datetime | None = None


@dataclass(frozen=True)
class ToolPolicyOverride:
    """工具风险/审批覆盖策略。"""

    risk: RiskLevelName
    approval: ApprovalModeName


class ConnectorError(Exception):
    """连接器执行异常，含分类错误码。"""

    def __init__(self, code: str, message: str) -> None:
        """保存分类错误码。

        Args:
            code: 稳定错误码。
            message: 人类可读的错误描述。
        """
        super().__init__(message)
        self.code = code
        self.message = message


@runtime_checkable
class Connector(Protocol):
    """统一连接器契约：不管底层是 CLI / MCP / API。"""

    key: str
    kind: ConnectorKind

    def config_fields(self) -> tuple[ConfigField, ...]:
        """声明配置项（含是否密钥），前端渲染 + 校验。

        Returns:
            配置字段列表。
        """
        ...

    async def connect(self) -> ConnectorStatusInfo:
        """建立会话/健康检查。

        Returns:
            连接状态信息。
        """
        ...

    async def list_tools(self) -> list[ConnectorToolDef]:
        """发现工具：MCP 走 list_tools()；CLI/API 为声明式定义。

        Returns:
            工具定义列表。
        """
        ...

    def tool_specs(
        self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]
    ) -> list[ToolSpec]:
        """把工具定义映射为 ToolSpec。

        Args:
            defs: 工具定义列表。
            policy: 工具风险/审批覆盖策略。

        Returns:
            可注册的 ToolSpec 列表。
        """
        ...

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        """执行一次外部调用。

        Args:
            name: 工具名（原始名称）。
            arguments: 调用参数。

        Returns:
            调用结果。
        """
        ...

    async def aclose(self) -> None:
        """释放子进程 / MCP 会话 / HTTP 连接。"""
        ...


def normalize_tool_name(name: str) -> str:
    """把外部工具名规范化为合法 ToolSpec key 片段。

    Args:
        name: 外部原始工具名。

    Returns:
        符合 `[a-z0-9_]` 的标识片段。
    """
    cleaned = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return cleaned.strip("_")[:48] or "tool"


def make_tool_key(connector_key: str, tool_name: str) -> str:
    """生成连接器工具的完整 key。

    Args:
        connector_key: 连接器标识。
        tool_name: 外部工具名。

    Returns:
        合法 ToolSpec key：`{connector_key}__{tool_name}`。
    """
    return f"{connector_key}__{normalize_tool_name(tool_name)}"
