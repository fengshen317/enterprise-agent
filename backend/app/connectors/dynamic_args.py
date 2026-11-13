"""JSON Schema → 动态 pydantic args_model 生成。

声明式工具（CLI/API）使用显式 pydantic 模型；
MCP 工具 schema 不可预知，用透传模型 + 轻量校验。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, create_model


class PassThroughArgs(BaseModel):
    """透传参数模型：接受任意字段，执行前轻量校验。

    用于 MCP 工具等 schema 不可预知的场景。
    """

    model_config = ConfigDict(extra="allow", strict=True)


def make_args_model(schema: dict[str, Any], tool_key: str) -> type[BaseModel]:
    """由 JSON Schema 生成 pydantic 参数模型。

    简单类型（string/number/boolean）→ 显式字段；
    复杂嵌套 → 降级为 Any + extra="forbid"。

    Args:
        schema: JSON Schema 对象。
        tool_key: 工具标识，用于生成模型名称。

    Returns:
        Pydantic 参数模型类。
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    if not isinstance(properties, dict):
        return _fallback_model(tool_key)

    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            return _fallback_model(tool_key)
        annotation = _map_type(prop_schema)
        is_required = prop_name in required
        if is_required:
            fields[prop_name] = (annotation, ...)
        elif "default" in prop_schema:
            fields[prop_name] = (annotation, prop_schema["default"])
        else:
            fields[prop_name] = (annotation | None, None)

    if not fields:
        return _fallback_model(tool_key)

    model_name = f"Args_{tool_key.replace('-', '_').replace('.', '_')}"
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )


def _map_type(schema: dict[str, Any]) -> type:
    """把 JSON Schema 类型映射为 Python 类型。

    Args:
        schema: 单个属性的 JSON Schema。

    Returns:
        Python 类型。
    """
    json_type = schema.get("type")
    if json_type == "string":
        return str
    if json_type == "integer":
        return int
    if json_type == "number":
        return float
    if json_type == "boolean":
        return bool
    if json_type == "array":
        return list[Any]
    return Any


def _fallback_model(tool_key: str) -> type[BaseModel]:
    """生成透传模型，接受任意参数。

    Args:
        tool_key: 工具标识。

    Returns:
        透传 pydantic 模型。
    """
    model_name = f"Args_{tool_key.replace('-', '_').replace('.', '_')}"
    return create_model(
        model_name,
        __config__=ConfigDict(extra="allow", strict=True),
        __base__=PassThroughArgs,
    )


def validate_arguments(
    schema: dict[str, Any], arguments: dict[str, Any], tool_key: str
) -> dict[str, Any]:
    """对 MCP 透传参数做轻量校验（required + type）。

    Args:
        schema: JSON Schema。
        arguments: 原始参数。
        tool_key: 工具标识。

    Returns:
        校验后的参数字典。

    Raises:
        ValueError: 必填参数缺失或类型不匹配。
    """
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict):
        return arguments

    for name in required:
        if name not in arguments:
            raise ValueError(f"缺少必填参数: {name}")

    for name, prop_schema in properties.items():
        if name not in arguments or not isinstance(prop_schema, dict):
            continue
        value = arguments[name]
        expected = prop_schema.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ValueError(f"参数 {name} 应为字符串")
        if expected == "integer" and not isinstance(value, int):
            raise ValueError(f"参数 {name} 应为整数")
        if expected == "number" and not isinstance(value, (int, float)):
            raise ValueError(f"参数 {name} 应为数字")
        if expected == "boolean" and not isinstance(value, bool):
            raise ValueError(f"参数 {name} 应为布尔值")

    return arguments
