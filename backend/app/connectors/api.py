"""API 连接器：声明式 REST 工具封装。

工具定义为声明式（请求模板 + 响应清洗），
连接器层只做"外部工具 → ToolSpec"翻译，
审批/审计/配额交给现有 ToolExecutor。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from app.capabilities.spec import ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
)

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 5000


@dataclass(frozen=True)
class ApiToolDef:
    """API 工具定义：声明式请求模板。

    name / description / input_schema 对应 ConnectorToolDef，
    额外的 method / path / headers / body_template 描述 HTTP 请求。
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    method: str = "POST"
    path: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body_template: dict[str, Any] = field(default_factory=dict)
    # dot notation 从响应提取字段，如 "results" 或 "data.items"
    response_filter: str | None = None
    risk: str = "external"
    approval: str = "always"

    def to_connector_def(self) -> ConnectorToolDef:
        """转换为 ConnectorToolDef。

        Returns:
            标准工具定义。
        """
        return ConnectorToolDef(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            risk=self.risk,  # type: ignore[arg-type]
            approval=self.approval,  # type: ignore[arg-type]
        )


class ApiConnector:
    """API 连接器：通过 HTTP 调用外部 REST 服务。

    工具定义为声明式（请求模板 + 响应清洗）。
    """

    def __init__(self, *, key: str, config: dict[str, Any], settings: Settings) -> None:
        """初始化连接器。

        Args:
            key: 连接器标识。
            config: 非密钥配置（base_url / auth / tools 等）。
            settings: 应用配置。
        """
        self.key = key
        self.kind = "api"
        self._config = config
        self._settings = settings
        self._client: httpx.AsyncClient | None = None

    def config_fields(self) -> tuple[ConfigField, ...]:
        """声明配置项。

        Returns:
            API 连接器通用配置字段。
        """
        return (
            ConfigField(key="base_url", label="API 地址", field_type="text", required=True),
            ConfigField(key="api_key", label="API Key", field_type="secret", is_secret=True),
        )

    async def connect(self) -> ConnectorStatusInfo:
        """建立 HTTP 客户端。

        Returns:
            连接状态。
        """
        base_url = str(self._config.get("base_url", "")).rstrip("/")
        if not base_url:
            return ConnectorStatusInfo(status="error", message="未配置 base_url")
        try:
            self._client = httpx.AsyncClient(base_url=base_url, timeout=15)
            return ConnectorStatusInfo(status="connected")
        except Exception as exc:
            return ConnectorStatusInfo(status="error", message=str(exc)[:200])

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
            tool_def = ApiToolDef(
                name=str(item.get("name", "")),
                description=str(item.get("description", "")),
                input_schema=item.get("input_schema", {}) if isinstance(item.get("input_schema"), dict) else {},
                method=str(item.get("method", "POST")),
                path=str(item.get("path", "")),
                headers=dict(item.get("headers", {})) if isinstance(item.get("headers"), dict) else {},
                body_template=item.get("body_template", {}) if isinstance(item.get("body_template"), dict) else {},
                response_filter=item.get("response_filter"),
                risk=str(item.get("risk", "external")),
                approval=str(item.get("approval", "always")),
            )
            defs.append(tool_def.to_connector_def())
        return defs

    def tool_specs(
        self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]
    ) -> list[ToolSpec]:
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
                """执行 API 调用。

                Args:
                    context: 工具执行上下文（连接器不使用）。
                    arguments: 调用参数。
                    _name: 工具原始名。

                Returns:
                    API 响应（经 response_filter 清洗）。
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
                    timeout_seconds=self._settings.tool_timeout_seconds + 10,
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

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行一次 HTTP API 调用。

        Args:
            name: 工具原始名。
            arguments: 调用参数。

        Returns:
            响应数据（经 response_filter 清洗后）。

        Raises:
            ConnectorError: HTTP 请求失败或响应异常。
        """
        tool_cfg = self._find_tool(name)
        if tool_cfg is None:
            raise ConnectorError("command_failed", f"工具 {name} 未定义")

        if self._client is None:
            await self.connect()
        if self._client is None:
            raise ConnectorError("command_failed", "HTTP 客户端未初始化")

        method = str(tool_cfg.get("method", "POST"))
        path = str(tool_cfg.get("path", ""))
        headers = dict(tool_cfg.get("headers", {}))
        body_template = tool_cfg.get("body_template", {})
        response_filter = tool_cfg.get("response_filter")
        no_body = bool(tool_cfg.get("no_body", False))

        # 模板参数替换
        url = _fill_template(path, arguments)
        body: dict[str, Any] | None = None
        if not no_body:
            body = _fill_template(body_template, arguments) if isinstance(body_template, dict) else None

        # 注入认证：优先 body_mapping（如 Tavily 的 api_key 在 body），否则注入 header
        api_key = self._get_secret("api_key")
        body_mapping = self._config.get("body_mapping", {})
        if api_key and isinstance(body_mapping, dict) and body_mapping.get("api_key") and body is not None:
            body[str(body_mapping["api_key"])] = api_key
        elif api_key:
            auth_header = str(self._config.get("auth_header", "Authorization"))
            auth_prefix = str(self._config.get("auth_prefix", "Bearer "))
            headers[auth_header] = f"{auth_prefix}{api_key}"

        try:
            kwargs: dict[str, Any] = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            response = await self._client.request(method.upper(), url, **kwargs)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            raise ConnectorError("timeout", f"API 调用超时: {path}") from None
        except httpx.HTTPStatusError as exc:
            raise ConnectorError("command_failed", f"API 返回 {exc.response.status_code}") from None
        except Exception as exc:
            raise ConnectorError("command_failed", f"API 调用失败: {exc}") from None

        # 响应清洗
        if response_filter and isinstance(data, dict):
            data = _extract_field(data, response_filter)

        # 截断超长响应
        result_str = str(data)
        if len(result_str) > MAX_RESPONSE_CHARS:
            return {"_truncated": True, "_preview": result_str[:MAX_RESPONSE_CHARS]}
        return data

    async def aclose(self) -> None:
        """释放 HTTP 连接。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _find_tool(self, name: str) -> dict[str, Any] | None:
        """按工具名查找配置。

        Args:
            name: 工具原始名。

        Returns:
            工具配置字典或空。
        """
        raw = self._config.get("tools", [])
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("name") == name:
                    return item
        return None

    def _get_secret(self, field_name: str) -> str:
        """获取密钥配置值。

        Args:
            field_name: 字段名。

        Returns:
            密钥值，未配置返回空字符串。
        """
        # 从 settings 读取（ConfigService 可热更新）
        settings_key = f"{self.key}_{field_name}"
        value = getattr(self._settings, settings_key, None)
        if value is not None:
            return str(value).strip()
        # 从 config 读取（非密钥场景）
        return str(self._config.get(field_name, "")).strip()


def _fill_template(template: str | dict[str, Any], arguments: dict[str, Any]) -> Any:
    """递归替换模板中的 {param} 占位符。

    模板恰好为 `{param}` 时保留原值类型（int/bool 等）。

    Args:
        template: 模板（字符串或字典）。
        arguments: 参数映射。

    Returns:
        替换后的值。
    """
    if isinstance(template, str):
        # 整体替换：保留原值类型
        for key, value in arguments.items():
            if template == "{" + key + "}":
                return value
        for key, value in arguments.items():
            template = template.replace(f"{{{key}}}", str(value))
        return template
    if isinstance(template, dict):
        return {k: _fill_template(v, arguments) for k, v in template.items()}
    return template


def _extract_field(data: Any, path: str) -> Any:
    """按 dot notation 从嵌套数据中提取字段。

    Args:
        data: 原始数据。
        path: 点分路径，如 "results" 或 "data.items"。

    Returns:
        提取的字段值；找不到返回原始数据。
    """
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return data
    return current if current is not None else data
