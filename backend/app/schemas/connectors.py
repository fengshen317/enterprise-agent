"""连接器接口契约：列表、配置与工具视图。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, SecretStr


class ConnectorHealthView(BaseModel):
    """供列表、详情和手动检查复用的健康信息。"""

    auth_status: str = "unknown"
    auth_action: str | None = None
    last_error_code: str | None = None
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None


class ConnectorView(ConnectorHealthView):
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


class PersonalField(BaseModel):
    """个人绑定表单字段，不携带已保存的值。"""

    key: str
    label: str
    secret: bool = False


class PersonalAccountView(BaseModel):
    """对当前工作空间成员可见的绑定信息。"""

    bound: bool
    fields: list[PersonalField]
    guide_url: str


class PersonalAccountInput(BaseModel):
    """写入凭证时禁止携带外部指定的用户或空间。"""

    model_config = ConfigDict(extra="forbid")
    credentials: dict[str, SecretStr] = Field(max_length=2)


class ImaProbeResult(BaseModel):
    """单个 ima 只读探针的公开结果。"""

    status: Literal["success", "error"]
    error_code: str | None = None
    diagnostic_id: UUID | None = None


class ImaAccountCheckResult(BaseModel):
    """分别报告当前成员的 ima 知识库和笔记连通性。"""

    knowledge_base: ImaProbeResult
    notes: ImaProbeResult


class ConnectorDetail(ConnectorHealthView):
    """连接器详情（含工具列表和配置项声明）。"""

    key: str
    kind: str
    name: str
    description: str
    icon: str
    enabled: bool
    status: str
    last_error: str | None = None
    config_fields: list[dict[str, Any]] = Field(default_factory=list)
    config_values: dict[str, str] = Field(default_factory=dict)
    tools: list[ConnectorToolView] = Field(default_factory=list)
    can_view_holdings: bool = False
    personal_account: PersonalAccountView | None = None


class YangJiBaoHoldingView(BaseModel):
    """绑定账号的基金持仓与行情。"""

    code: str
    name: str
    holding_amount: FiniteFloat | None
    estimated_change_percent: FiniteFloat | None
    latest_nav_change_percent: FiniteFloat | None
    quote_time: str | None
    nav_date: str | None
    share: FiniteFloat | None = None
    cost_amount: FiniteFloat | None = None
    cost_nav: FiniteFloat | None = None
    estimated_amount: FiniteFloat | None = None
    latest_nav_amount: FiniteFloat | None = None
    estimated_today_profit_amount: FiniteFloat | None = None


class YangJiBaoPortfolioView(BaseModel):
    """页面一次同步得到的持仓列表。"""

    synced_at: datetime
    fund_count: int
    funds: list[YangJiBaoHoldingView]


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


class ConnectorTestResult(ConnectorHealthView):
    """连接器测试连接结果。"""

    status: str
    message: str
    tool_count: int = 0
