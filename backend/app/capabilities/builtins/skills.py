"""按需加载技能正文，正文只进入模型上下文，不进入审计。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.chat_agent import LoadSkillArgs

LOAD_SKILL_TOOL = "load_skill"


def load_skill_spec(settings: Settings) -> ToolSpec:
    """构造按需技能加载工具。

    Args:
        settings: 单次正文与调用次数预算。

    Returns:
        系统工具声明。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """返回已绑定技能的正文，缺少绑定时明确拒绝。

        Args:
            context: 可信运行上下文。
            arguments: 技能 key。

        Returns:
            技能元数据与截断后的正文。
        """
        if not isinstance(arguments, LoadSkillArgs):
            raise TypeError("技能加载参数类型不正确")
        skill = context.skills.get(arguments.key)
        if skill is None:
            raise ApplicationError("SKILL_NOT_FOUND", "当前 Agent 未绑定该技能", 404)
        content = skill.content[: settings.skill_load_max_characters]
        return {
            "key": skill.key,
            "name": skill.name,
            "version": skill.version,
            "content": content,
            "truncated": len(content) < len(skill.content),
        }

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """只记录技能标识与长度，不保存工作正文。

        Args:
            arguments: 技能 key。
            result: 加载结果。

        Returns:
            脱敏审计摘要。
        """
        assert isinstance(arguments, LoadSkillArgs)
        data = result if isinstance(result, dict) else {}
        return {
            "key": arguments.key,
            "version": data.get("version"),
            "length": len(str(data.get("content", ""))),
        }

    return ToolSpec(
        key=LOAD_SKILL_TOOL,
        version=1,
        display_name="加载技能",
        description="加载当前 Agent 已绑定的按需技能正文。需要遵循某个 SOP 时调用。",
        args_model=LoadSkillArgs,
        risk="read",
        approval="never",
        timeout_seconds=2,
        quota_per_run=settings.skill_load_max_calls,
        handler=handler,
        audit=audit,
        child_allowed=False,
        system=True,
    )
