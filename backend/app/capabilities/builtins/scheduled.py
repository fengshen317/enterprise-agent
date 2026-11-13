"""从对话创建定时任务；固定当前对话，写操作必须逐次审批。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.schedule import CreateScheduledTaskArgs, ScheduleInput

CREATE_SCHEDULE_TOOL = "create_scheduled_task"


def scheduled_spec(settings: Settings) -> ToolSpec:
    """构造创建定时任务的写工具。

    Args:
        settings: 工具超时配置。

    Returns:
        写工具声明。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """将任务写入当前对话，不允许模型指定其他对话。

        Args:
            context: 可信运行上下文。
            arguments: 任务内容与 Cron。

        Returns:
            新建定时任务。

        Raises:
            ApplicationError: 定时任务服务不可用。
        """
        if not isinstance(arguments, CreateScheduledTaskArgs):
            raise TypeError("定时任务参数类型不正确")
        if context.schedule_store is None:
            raise ApplicationError("SCHEDULE_DISABLED", "定时任务功能未启用", 503)
        view = await context.schedule_store.create(
            context.scope,
            ScheduleInput(
                name=arguments.name,
                prompt=arguments.prompt,
                conversation_id=context.conversation_id,
                cron_expression=arguments.cron_expression,
                timezone=arguments.timezone,
                enabled=arguments.enabled,
            ),
        )
        return view.model_dump(mode="json")

    def preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        """生成审批卡片所需的脱敏影响说明。

        Args:
            arguments: 任务内容。
            context: 可信运行上下文。

        Returns:
            审批预览。
        """
        assert isinstance(arguments, CreateScheduledTaskArgs)
        return {
            "summary": f"创建定时任务「{arguments.name}」",
            "cron": arguments.cron_expression,
            "timezone": arguments.timezone or "工作空间默认时区",
            "prompt": arguments.prompt[:200],
            "effect": "将向当前对话追加定时运行",
        }

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """审计只保存任务标识与调度摘要，不保存完整问题。

        Args:
            arguments: 任务内容。
            result: 创建结果。

        Returns:
            脱敏审计摘要。
        """
        assert isinstance(arguments, CreateScheduledTaskArgs)
        data = result if isinstance(result, dict) else {}
        return {
            "schedule_id": data.get("id"),
            "name": arguments.name,
            "cron": arguments.cron_expression,
            "timezone": arguments.timezone,
            "prompt": arguments.prompt[:200],
        }

    return ToolSpec(
        key=CREATE_SCHEDULE_TOOL,
        version=1,
        display_name="创建定时任务",
        description="按 Cron 在当前对话创建定时问答任务。执行前会请求用户审批。",
        args_model=CreateScheduledTaskArgs,
        risk="write",
        approval="always",
        timeout_seconds=5,
        quota_per_run=1,
        handler=handler,
        audit=audit,
        approval_preview=preview,
        child_allowed=False,
    )
