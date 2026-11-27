"""为腾讯个人应用提供一致的可信身份、工具装配和审计边界。"""

from collections.abc import Awaitable, Callable, Mapping

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors import ima, qq_mail, tavily, tencent_news
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorKind,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
    make_tool_key,
)
from app.connectors.dynamic_args import make_args_model
from app.services.personal_credentials import read_credentials

AdapterCall = Callable[[str, dict[str, object], dict[str, str]], Awaitable[object]]
_TOOL_TIMEOUT_SECONDS = 45
_TOOL_QUOTA_PER_RUN = 8
ADAPTERS: dict[str, tuple[tuple[ConnectorToolDef, ...], AdapterCall]] = {
    "qq_mail": (qq_mail.TOOLS, qq_mail.invoke),
    "ima": (ima.TOOLS, ima.invoke),
    "tencent_news": (tencent_news.TOOLS, tencent_news.invoke),
    "tavily_search": (tavily.TOOLS, tavily.invoke),
}


class PersonalConnector:
    """工具可以共享定义，凭证仅在每次调用时按 Run 身份获取。"""

    def __init__(self, key: str) -> None:
        """保存无凭证的适配器定义。"""
        self.key = key
        self.kind: ConnectorKind = "cli" if key == "tencent_news" else "api"
        self.definitions, self.invoke = ADAPTERS[key]

    def config_fields(self) -> tuple[ConfigField, ...]:
        """个人密钥只走成员绑定端点。"""
        return ()

    async def connect(self) -> ConnectorStatusInfo:
        """服务级健康检查不代表任一成员已获上游授权。"""
        return await self.check_health()

    async def check_health(self) -> ConnectorStatusInfo:
        """只报告适配器装配状态，个人授权由调用者单独验证。"""
        return ConnectorStatusInfo(status="connected", auth_status="unknown")

    async def list_tools(self) -> list[ConnectorToolDef]:
        """返回经过筛选的只读动作。"""
        return list(self.definitions)

    def tool_specs(self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]) -> list[ToolSpec]:
        """使用运行上下文确定外部账号，同时保留逐工具审批策略。"""
        return [self._spec(definition, policy.get(definition.name)) for definition in defs]

    def _spec(self, definition: ConnectorToolDef, override: ToolPolicyOverride | None) -> ToolSpec:
        """闭包只捕获工具名，避免缓存用户凭证或复用上次调用身份。"""
        key = make_tool_key(self.key, definition.name)
        risk = override.risk if override else definition.risk

        async def handler(context: ToolContext, arguments: BaseModel) -> object:
            """仅在权限复核后使用该成员的凭证发起一次请求。"""
            async with context.factory() as session:
                credentials = await read_credentials(session, context.settings, context.scope, self.key)
            values = arguments.model_dump(exclude_none=True)
            if self.key == "tavily_search":
                values["max_results"] = min(int(values.get("max_results", 5)), context.settings.web_search_max_results)
                values["_timeout_seconds"] = context.settings.web_search_timeout_seconds
            return await self.invoke(definition.name, values, credentials)

        def audit(arguments: BaseModel, result: object) -> dict[str, object]:
            """执行记录仅保存动作名，避免泄露邮件、笔记或查询条件。"""
            return {"action": definition.name}

        return ToolSpec(
            key=key,
            version=1,
            display_name=definition.name,
            description=definition.description,
            args_model=make_args_model(definition.input_schema, key),
            risk=risk,
            approval=override.approval if override else definition.approval,
            timeout_seconds=_TOOL_TIMEOUT_SECONDS,
            quota_per_run=_TOOL_QUOTA_PER_RUN,
            handler=handler,
            audit=audit,
            source="connector",
            connector_key=self.key,
            child_allowed=risk == "read",
            input_schema_override=definition.input_schema,
        )

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        """不接受缺少可信运行身份的直接调用。"""
        raise ConnectorError("permission_denied", "个人工具需要当前工作空间用户身份")

    async def aclose(self) -> None:
        """每次调用自行释放连接，无长期共享会话。"""
