"""连接器接口契约：列表、配置与工具视图。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectorView(BaseModel):
    """连接器列表项。"""

    key: str
    kind: str
    name: str
    description: str
    icon: str
    enabled: bool
    status: str
    tool_count: int = 0
    last_error: str | None = None


class ConnectorToolView(BaseModel):
    """连接器工具目录项。"""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk: str = "external"
    approval: str = "always"


class ConnectorDetail(BaseModel):
    """连接器详情（含工具列表和配置项声明）。"""

    key: str
    kind: str
    name: str
    description: str
    icon: str
    enabled: bool
    status: str
    last_error: str | None = None
    last_checked_at: datetime | None = None
    config_fields: list[dict[str, Any]] = Field(default_factory=list)
    config_values: dict[str, str] = Field(default_factory=dict)
    tools: list[ConnectorToolView] = Field(default_factory=list)


class ConnectorConfigInput(BaseModel):
    """连接器配置更新（非密钥字段）。"""

    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorSecretsInput(BaseModel):
    """连接器密钥更新（单独提交，不回显明文）。"""

    model_config = ConfigDict(extra="forbid")
    secrets: dict[str, str] = Field(default_factory=dict)


class ConnectorEnabledInput(BaseModel):
    """连接器启停。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ConnectorTestResult(BaseModel):
    """连接器测试连接结果。"""

    status: str
    message: str
    tool_count: int = 0
