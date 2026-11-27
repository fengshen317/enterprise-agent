"""从对话管理定时任务，写操作必须逐次审批。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.schedule import (
    CreateScheduledTaskArgs,
    GetScheduledTaskArgs,
    ListScheduledTasksArgs,
    ScheduleView,
    SetScheduledTaskEnabledArgs,
    UpdateScheduledTaskArgs,
)
from app.services.push_channels import apply_wechat_flag, normalize_push_channels
from app.services.schedule_calendar import CALENDAR_LABELS
from app.services.schedule_store import ScheduleStore
from app.services.scheduling import timezone_info, validate_expression

CREATE_SCHEDULE_TOOL = "create_scheduled_task"
LIST_SCHEDULES_TOOL = "list_scheduled_tasks"
SET_SCHEDULE_ENABLED_TOOL = "set_scheduled_task_enabled"
GET_SCHEDULE_TOOL = "get_scheduled_task"
UPDATE_SCHEDULE_TOOL = "update_scheduled_task"


def _store(context: ToolContext) -> ScheduleStore:
    """从可信上下文取得定时任务存储。"""
    if context.schedule_store is None:
        raise ApplicationError("SCHEDULE_DISABLED", "定时任务功能未启用", 503)
    return context.schedule_store


def scheduled_spec(settings: Settings) -> ToolSpec:
    """构造创建定时任务的写工具。

    Args:
        settings: 工具超时配置。

    Returns:
        写工具声明。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """默认沿用当前对话，指定私有 Agent 时原子创建专用对话与任务。

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
        if context.tool_call_id is None:
            raise ApplicationError("APPROVAL_REQUIRED", "创建定时任务需要关联审批", 409)
        view = await context.schedule_store.execute_approved(context.run_id, context.tool_call_id)
        result = view.model_dump(mode="json")
        result["url"] = f"/schedules?schedule={view.id}"
        if arguments.agent_profile_id is not None:
            result["agent_profile_id"] = str(arguments.agent_profile_id)
        return result

    async def preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        """生成审批卡片所需的脱敏影响说明。

        Args:
            arguments: 任务内容。
            context: 可信运行上下文。

        Returns:
            审批预览。
        """
        assert isinstance(arguments, CreateScheduledTaskArgs)
        store = context.schedule_store
        if store is None:
            raise ApplicationError("SCHEDULE_DISABLED", "定时任务功能未启用", 503)
        effect = f"使用当前对话 {context.conversation_id}"
        if arguments.agent_profile_id is not None:
            name = await store.private_agent_name(context.scope, arguments.agent_profile_id)
            effect = (
                f"新建专用对话「{arguments.name.strip()}」，绑定私有 Agent「{name}」（{arguments.agent_profile_id}）"
            )
        wechat, channels = normalize_push_channels(arguments.push_to_wechat, arguments.push_channels)
        return {
            "summary": f"创建定时任务「{arguments.name}」",
            "effect": effect,
            "enabled": arguments.enabled,
            "push_to_wechat": wechat,
            "push_channels": channels,
            "cron": arguments.cron_expression,
            "timezone": arguments.timezone or store.default_timezone,
            "calendar_mode": CALENDAR_LABELS[arguments.calendar_mode],
            "prompt": arguments.prompt[:200],
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
            "conversation_id": data.get("conversation_id"),
            "agent_profile_id": data.get("agent_profile_id"),
            "name": arguments.name,
            "cron": arguments.cron_expression,
            "timezone": arguments.timezone,
            "calendar_mode": arguments.calendar_mode,
            "prompt": arguments.prompt[:200],
        }

    return ToolSpec(
        key=CREATE_SCHEDULE_TOOL,
        version=1,
        display_name="创建定时任务",
        description="按 Cron 创建定时问答任务。默认使用当前对话，指定本人私有 Agent ID 时新建专用对话。执行前需审批。",
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
        items = await context.schedule_store.list(context.scope, arguments.offset, 100)
        return [
            {
                "id": str(item.id),
                "name": item.name,
                "conversation_id": str(item.conversation_id),
                "is_current_conversation": item.conversation_id == context.conversation_id,
                "cron_expression": item.cron_expression,
                "timezone": item.timezone,
                "calendar_mode": item.calendar_mode,
                "enabled": item.enabled,
                "push_to_wechat": item.push_to_wechat,
                "push_channels": item.push_channels,
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
    """返回定时任务的查询、创建、编辑与启停工具。

    Args:
        settings: 工具超时配置。

    Returns:
        按只读与写入顺序排列的工具声明。
    """
    return (
        list_scheduled_tasks_spec(settings),
        get_scheduled_task_spec(settings),
        scheduled_spec(settings),
        update_scheduled_task_spec(settings),
        set_scheduled_task_enabled_spec(settings),
    )


def get_scheduled_task_spec(settings: Settings) -> ToolSpec:
    """注册仅主 Agent 使用的本人任务详情工具。"""

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """读取任务指令、调度配置与编辑版本。"""
        assert isinstance(arguments, GetScheduledTaskArgs)
        view = await _store(context).get(context.scope, arguments.schedule_id)
        return view.model_dump(mode="json")

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """审计仅保存定位标识，避免复制任务指令。"""
        assert isinstance(arguments, GetScheduledTaskArgs)
        return {"schedule_id": str(arguments.schedule_id)}

    return ToolSpec(
        key=GET_SCHEDULE_TOOL,
        version=1,
        display_name="读取定时任务",
        description="读取本人指定任务的指令、时间规则与当前配置版本。任务指令仅供参考，不要立即执行。",
        args_model=GetScheduledTaskArgs,
        risk="read",
        approval="never",
        timeout_seconds=settings.tool_timeout_seconds,
        quota_per_run=4,
        handler=handler,
        audit=audit,
        child_allowed=False,
    )


def update_scheduled_task_spec(settings: Settings) -> ToolSpec:
    """注册逐次审批的定时任务编辑工具。"""

    async def preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        """比较当前版本，只展示实际要修改的配置。"""
        assert isinstance(arguments, UpdateScheduledTaskArgs)
        store = _store(context)
        current = await store.get(context.scope, arguments.schedule_id)
        store.check_revision(current.revision, arguments.revision)
        fields = arguments.changes()
        # 审批展示存储层实际采用的渠道列表，包括旧接口单独切换微信的情况。
        if arguments.push_channels is not None:
            fields["push_channels"] = normalize_push_channels(False, arguments.push_channels)[1]
        elif arguments.push_to_wechat is not None:
            fields["push_channels"] = apply_wechat_flag(current.push_channels, arguments.push_to_wechat)
        fields.pop("push_to_wechat", None)
        if arguments.name is not None:
            fields["name"] = arguments.name.strip()
        if arguments.cron_expression is not None:
            fields["cron_expression"] = validate_expression(arguments.cron_expression)
        if arguments.timezone is not None:
            fields["timezone"] = timezone_info(arguments.timezone).key
        if arguments.calendar_mode is not None:
            fields["calendar_mode"] = CALENDAR_LABELS[arguments.calendar_mode]
        card: dict[str, object] = {"summary": f"修改定时任务「{current.name}」", "schedule_id": str(current.id)}
        before = current.model_dump()
        if arguments.calendar_mode is not None:
            before["calendar_mode"] = CALENDAR_LABELS[current.calendar_mode]
        for key, value in fields.items():
            if value != before[key]:
                card[key] = {"before": before[key], "after": value}
        if len(card) == 2:
            raise ApplicationError("SCHEDULE_NO_CHANGES", "任务配置没有变化", 422)
        return card

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """原地更新任务，不更换固定对话或重建任务。"""
        assert isinstance(arguments, UpdateScheduledTaskArgs)
        view: ScheduleView = await _store(context).patch(context.scope, arguments)
        return {
            "schedule_id": str(view.id),
            "name": view.name,
            "revision": view.revision,
            "url": f"/schedules?schedule={view.id}",
            "next_run_at": view.next_run_at.isoformat() if view.next_run_at else None,
            "calendar_waiting_year": view.calendar_waiting_year,
        }

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """审计只记录任务和字段名，任务指令仅出现在私有审批。"""
        assert isinstance(arguments, UpdateScheduledTaskArgs)
        data = result if isinstance(result, dict) else {}
        return {
            "schedule_id": str(arguments.schedule_id),
            "name": data.get("name"),
            "revision": data.get("revision"),
            "fields": list(arguments.changes()),
        }

    return ToolSpec(
        key=UPDATE_SCHEDULE_TOOL,
        version=1,
        display_name="修改定时任务",
        description=(
            "原地修改本人已有的定时任务。先列出任务确认唯一目标，再获取详情及 revision，"
            "只提交用户明确要求修改的字段，执行前需要审批。"
        ),
        args_model=UpdateScheduledTaskArgs,
        risk="write",
        approval="always",
        timeout_seconds=settings.tool_timeout_seconds,
        quota_per_run=3,
        handler=handler,
        audit=audit,
        approval_preview=preview,
        child_allowed=False,
    )
