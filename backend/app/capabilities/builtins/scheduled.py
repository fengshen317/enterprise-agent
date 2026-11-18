"""从对话创建定时任务；固定当前对话，写操作必须逐次审批。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.schedule import (
    CreateScheduledTaskArgs,
    ListScheduledTasksArgs,
    ScheduleInput,
    SetScheduledTaskEnabledArgs,
)

CREATE_SCHEDULE_TOOL = "create_scheduled_task"
LIST_SCHEDULES_TOOL = "list_scheduled_tasks"
SET_SCHEDULE_ENABLED_TOOL = "set_scheduled_task_enabled"


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
                push_to_wechat=arguments.push_to_wechat,
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


def list_scheduled_tasks_spec(settings: Settings) -> ToolSpec:
    """构造本人定时任务只读工具。

    Args:
        settings: 工具超时配置。

    Returns:
        只读工具声明。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """列出当前空间任务，并标出当前对话对应的任务。

        Args:
            context: 可信运行上下文。
            arguments: 空参数。

        Returns:
            当前用户可见的任务摘要。
        """
        if not isinstance(arguments, ListScheduledTasksArgs):
            raise TypeError("定时任务列表参数类型不正确")
        if context.schedule_store is None:
            raise ApplicationError("SCHEDULE_DISABLED", "定时任务功能未启用", 503)
        items = await context.schedule_store.list(context.scope, 0, 100)
        return [
            {
                "id": str(item.id),
                "name": item.name,
                "conversation_id": str(item.conversation_id),
                "is_current_conversation": item.conversation_id == context.conversation_id,
                "enabled": item.enabled,
                "push_to_wechat": item.push_to_wechat,
                "next_run_at": item.next_run_at.isoformat() if item.next_run_at is not None else None,
            }
            for item in items
        ]

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """只记录返回数量，不复制任务内容。

        Args:
            arguments: 空参数。
            result: 工具结果。

        Returns:
            审计摘要。
        """
        del arguments
        return {"count": len(result) if isinstance(result, list) else 0}

    return ToolSpec(
        key=LIST_SCHEDULES_TOOL,
        version=1,
        display_name="列出定时任务",
        description="列出本人当前空间的定时任务，并标出哪些任务属于当前对话。",
        args_model=ListScheduledTasksArgs,
        risk="read",
        approval="never",
        timeout_seconds=settings.tool_timeout_seconds,
        quota_per_run=2,
        handler=handler,
        audit=audit,
    )


def set_scheduled_task_enabled_spec(settings: Settings) -> ToolSpec:
    """构造任务启停写工具，执行仍需逐次审批。

    Args:
        settings: 工具超时配置。

    Returns:
        写工具声明。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """启用或停用一条本人任务。

        Args:
            context: 可信运行上下文。
            arguments: 任务标识和目标状态。

        Returns:
            更新后的任务视图。
        """
        if not isinstance(arguments, SetScheduledTaskEnabledArgs):
            raise TypeError("定时任务启停参数类型不正确")
        if context.schedule_store is None:
            raise ApplicationError("SCHEDULE_DISABLED", "定时任务功能未启用", 503)
        view = await context.schedule_store.set_enabled(context.scope, arguments.schedule_id, arguments.enabled)
        return view.model_dump(mode="json")

    def preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        """明确展示任务标识和启停影响。

        Args:
            arguments: 任务标识和状态。
            context: 可信运行上下文。

        Returns:
            审批预览。
        """
        del context
        assert isinstance(arguments, SetScheduledTaskEnabledArgs)
        action = "启用" if arguments.enabled else "停用"
        return {
            "summary": f"{action}定时任务",
            "schedule_id": str(arguments.schedule_id),
            "effect": "启用后会继续按 Cron 触发" if arguments.enabled else "停用后不再自动触发",
        }

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """记录任务标识和最终启停状态。

        Args:
            arguments: 任务标识和状态。
            result: 工具结果。

        Returns:
            审计摘要。
        """
        assert isinstance(arguments, SetScheduledTaskEnabledArgs)
        data = result if isinstance(result, dict) else {}
        return {
            "schedule_id": str(arguments.schedule_id),
            "enabled": data.get("enabled", arguments.enabled),
        }

    return ToolSpec(
        key=SET_SCHEDULE_ENABLED_TOOL,
        version=1,
        display_name="启停定时任务",
        description="启用或停用本人定时任务；先用 list_scheduled_tasks 确认目标，多个候选时必须询问用户。",
        args_model=SetScheduledTaskEnabledArgs,
        risk="write",
        approval="always",
        timeout_seconds=settings.tool_timeout_seconds,
        quota_per_run=1,
        handler=handler,
        audit=audit,
        approval_preview=preview,
        child_allowed=False,
    )


def scheduled_specs(settings: Settings) -> tuple[ToolSpec, ...]:
    """返回定时任务的查询、创建与启停工具。

    Args:
        settings: 工具超时配置。

    Returns:
        按只读、创建、启停排列的工具声明。
    """
    return list_scheduled_tasks_spec(settings), scheduled_spec(settings), set_scheduled_task_enabled_spec(settings)
