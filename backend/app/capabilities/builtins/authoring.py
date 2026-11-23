"""对话中创建私有技能、发布草稿和创建私有 Agent 的逐次审批工具。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.capabilities.spec import AgentAssembly, ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.agent import Activation, AgentInput, AgentSkillInput, AgentView
from app.schemas.skill import SkillInput, SkillPublishInput, SkillView
from app.services.skills import SkillService

CREATE_PRIVATE_SKILL = "create_private_skill"
PUBLISH_PRIVATE_SKILL = "publish_private_skill"
CREATE_PRIVATE_AGENT = "create_private_agent"

_AUTHORING_KEYS = {CREATE_PRIVATE_SKILL, PUBLISH_PRIVATE_SKILL, CREATE_PRIVATE_AGENT}


class _ResourceArgs(BaseModel):
    """模型只能提供配置内容，作用域由服务端派生。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    tool_keys: list[str] = Field(default_factory=list, max_length=20)


class CreatePrivateSkillArgs(_ResourceArgs):
    """创建第一版私有 Markdown 草稿。"""

    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    content_md: str = Field(min_length=1, max_length=20000)


class PublishPrivateSkillArgs(SkillPublishInput):
    """使用创建结果中的标识和版本定位待发布草稿。"""

    skill_id: UUID


class SkillVersionBinding(BaseModel):
    """显示指定技能和不可变的发布版本。"""

    model_config = ConfigDict(extra="forbid")
    skill_id: UUID
    version: int = Field(ge=1)
    activation: Activation = "always"


class CreatePrivateAgentArgs(_ResourceArgs):
    """用当前 Run 可用的工具和明确的技能版本创建私有 Agent。"""

    instructions: str = Field(min_length=1, max_length=8000)
    skills: list[SkillVersionBinding] = Field(default_factory=list, max_length=20)


def _assembly(context: ToolContext, tool_keys: list[str]) -> AgentAssembly:
    """复核业务工具的运行白名单、现行注册和账号限制。

    Args:
        context: 可信运行上下文。
        tool_keys: 拟绑定工具。

    Returns:
        当前运行的能力快照。
    """
    assembly = context.assembly
    if assembly is None:
        raise ApplicationError("AUTHORING_SCOPE_REQUIRED", "缺少当前运行的能力范围", 403)
    for key in tool_keys:
        registered = context.executor.registry.resolve(key)
        granted = assembly.tool_map.get(key)
        if (
            key in _AUTHORING_KEYS
            or granted is None
            or registered is None
            or granted.system
            or registered.system
            or not granted.allows_user(context.scope.user_id)
            or not registered.allows_user(context.scope.user_id)
        ):
            raise ApplicationError("TOOL_NOT_ALLOWED", f"当前运行无权绑定该业务工具：{key}", 403)
    return assembly


def _knowledge(assembly: AgentAssembly) -> tuple[Literal["selected", "all_readable"], list[UUID]]:
    """继承运行的知识库边界，空选区无法持久化时拒绝创建。

    Args:
        assembly: 已确认的运行快照。

    Returns:
        适用于配置服务的模式和库标识。
    """
    selected = assembly.selected_kb_ids()
    if selected is None and assembly.knowledge_scope.get("mode") == "all_readable":
        return "all_readable", []
    if not selected:
        raise ApplicationError("KNOWLEDGE_SCOPE_EMPTY", "当前知识库范围无法用于创建配置", 422)
    return "selected", sorted(selected)


def _skill_service(context: ToolContext) -> SkillService:
    """使用本次运行的数据库和工具目录构造技能服务。

    Args:
        context: 可信运行上下文。

    Returns:
        本次调用使用的技能服务。
    """
    return SkillService(context.factory, context.settings, context.executor.registry)


def _result(view: AgentView | SkillView) -> dict[str, object]:
    """生成可审计的结果摘要和详情地址。

    Args:
        view: 已持久化的资源视图。

    Returns:
        可向模型和审计提供的字段。
    """
    result: dict[str, object] = {
        "id": str(view.id),
        "name": view.name,
        "visibility": view.visibility,
        "status": view.status,
    }
    if isinstance(view, SkillView):
        result.update(key=view.key, version=view.current_version, url=f"/capabilities/skills/{view.id}")
    else:
        result.update(version=view.version, url=f"/agents/{view.id}")
    return result


def _audit(arguments: BaseModel, result: object) -> dict[str, object]:
    """只允许资源定位字段进入审计，防止后续扩充返回值时泄露指令。

    Args:
        arguments: 工具原始参数。
        result: 写入后的公开摘要。

    Returns:
        可持久化的审计字段。
    """
    fields = ("id", "name", "key", "version", "visibility", "status", "url")
    return {key: result[key] for key in fields if isinstance(result, dict) and key in result}


def _create_preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
    """展示用户将批准的完整内容、版本及本次运行的能力边界。

    Args:
        arguments: 待创建配置。
        context: 可信运行上下文。

    Returns:
        审批卡片内容。
    """
    assert isinstance(arguments, (CreatePrivateAgentArgs, CreatePrivateSkillArgs))
    assembly = _assembly(context, arguments.tool_keys)
    _knowledge(assembly)
    card: dict[str, object] = {
        **arguments.model_dump(mode="json"),
        "visibility": "private",
        "version": 1,
        "knowledge_scope": assembly.knowledge_scope,
    }
    if isinstance(arguments, CreatePrivateAgentArgs):
        card.update(
            summary=f"创建私有 Agent「{arguments.name}」",
            allow_planning=assembly.allow_planning,
            allow_delegation=assembly.allow_delegation,
        )
    else:
        card["summary"] = f"创建私有技能草稿「{arguments.name}」"
    return card


async def _create_skill(context: ToolContext, arguments: BaseModel) -> object:
    """创建仅当前用户可见的第一版草稿。

    Args:
        context: 可信运行上下文。
        arguments: 草稿配置。

    Returns:
        草稿摘要与详情地址。
    """
    assert isinstance(arguments, CreatePrivateSkillArgs)
    mode, ids = _knowledge(_assembly(context, arguments.tool_keys))
    body = SkillInput(**arguments.model_dump(), visibility="private", knowledge_mode=mode, knowledge_base_ids=ids)
    return _result(await _skill_service(context).create_skill(context.scope, body))


async def _private_draft(context: ToolContext, arguments: PublishPrivateSkillArgs) -> SkillView:
    """确认待发布版本可见且在当前工具、知识库边界内。

    Args:
        context: 可信运行上下文。
        arguments: 用户要发布的草稿标识。

    Returns:
        当前用户有权批准的草稿内容。
    """
    skill = await _skill_service(context).get_skill(context.scope, arguments.skill_id)
    if not skill.is_owner or skill.visibility != "private":
        raise ApplicationError("SKILL_ACCESS_DENIED", "仅可发布本人的私有技能", 403)
    if skill.current_version != arguments.version or skill.status != "draft":
        raise ApplicationError("SKILL_VERSION_CONFLICT", "指定版本已不是当前草稿", 409)
    mode, ids = _knowledge(_assembly(context, skill.tool_keys))
    if mode == "selected":
        skill_ids = skill.knowledge_scope.get("kb_ids")
        if (
            skill.knowledge_scope.get("mode") != "selected"
            or not isinstance(skill_ids, list)
            or not set(map(str, skill_ids)).issubset(map(str, ids))
        ):
            raise ApplicationError("SKILL_KNOWLEDGE_SCOPE", "技能知识库范围超出本次运行", 403)
    return skill


async def _publish_preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
    """读取待发布版本的实际正文供用户核对。

    Args:
        arguments: 技能标识与目标版本。
        context: 可信运行上下文。

    Returns:
        从数据库读取的审批卡片内容。
    """
    assert isinstance(arguments, PublishPrivateSkillArgs)
    skill = await _private_draft(context, arguments)
    return {
        "summary": f"发布私有技能「{skill.name}」",
        "skill_id": str(skill.id),
        "key": skill.key,
        "name": skill.name,
        "version": arguments.version,
        "visibility": skill.visibility,
        "description": skill.description,
        "content_md": skill.content_md,
        "tool_keys": skill.tool_keys,
        "knowledge_scope": skill.knowledge_scope,
    }


async def _publish_skill(context: ToolContext, arguments: BaseModel) -> object:
    """发布审批指定的草稿，事务内再次核验私有归属和当前版本。

    Args:
        context: 可信运行上下文。
        arguments: 技能标识与目标版本。

    Returns:
        已发布版本摘要。
    """
    assert isinstance(arguments, PublishPrivateSkillArgs)
    await _private_draft(context, arguments)
    skill = await _skill_service(context).publish_skill(
        context.scope, arguments.skill_id, arguments.version, private_draft_only=True
    )
    return _result(skill)


async def _create_agent(context: ToolContext, arguments: BaseModel) -> object:
    """在当前运行权限内创建私有 Agent，技能版本随绑定一起写入。

    Args:
        context: 可信运行上下文。
        arguments: 私有 Agent 的配置。

    Returns:
        配置摘要与详情地址。
    """
    from app.services.agents import AgentService

    assert isinstance(arguments, CreatePrivateAgentArgs)
    assembly = _assembly(context, arguments.tool_keys)
    mode, ids = _knowledge(assembly)
    body = AgentInput(
        name=arguments.name,
        description=arguments.description,
        instructions=arguments.instructions,
        tool_keys=arguments.tool_keys,
        skills=[
            AgentSkillInput(id=item.skill_id, version=item.version, activation=item.activation)
            for item in arguments.skills
        ],
        visibility="private",
        allow_planning=assembly.allow_planning,
        allow_delegation=assembly.allow_delegation,
        model=assembly.model,
        knowledge_mode=mode,
        knowledge_base_ids=ids,
    )
    service = AgentService(context.factory, context.settings, context.executor.registry)
    return _result(await service.create_agent(context.scope, body))


def authoring_specs(settings: Settings) -> tuple[ToolSpec, ...]:
    """构建仅主 Agent 可用且每次都需要用户审批的私有配置工具。

    Args:
        settings: 部署的工具超时设置。

    Returns:
        创建技能、发布技能和创建 Agent 的声明。
    """
    return tuple(
        ToolSpec(
            key=key,
            version=1,
            display_name=name,
            description=description,
            args_model=args,
            risk="write",
            approval="always",
            timeout_seconds=settings.tool_timeout_seconds,
            quota_per_run=1,
            handler=handler,
            audit=_audit,
            approval_preview=preview,
            child_allowed=False,
        )
        for key, name, description, args, handler, preview in (
            (
                CREATE_PRIVATE_SKILL,
                "创建私有技能草稿",
                "将用户确认的流程保存为私有技能的第一版草稿。",
                CreatePrivateSkillArgs,
                _create_skill,
                _create_preview,
            ),
            (
                PUBLISH_PRIVATE_SKILL,
                "发布私有技能",
                "按技能 ID 和版本发布本人的私有草稿，使其可绑定到 Agent。",
                PublishPrivateSkillArgs,
                _publish_skill,
                _publish_preview,
            ),
            (
                CREATE_PRIVATE_AGENT,
                "创建私有 Agent",
                "按明确指令、当前可用业务工具和已发布技能版本创建私有 Agent。",
                CreatePrivateAgentArgs,
                _create_agent,
                _create_preview,
            ),
        )
    )
