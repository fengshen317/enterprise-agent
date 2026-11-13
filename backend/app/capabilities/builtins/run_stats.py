"""运行统计工具：只返回聚合值，不读取消息正文。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.schemas.tools import TOOL_NAME, RunStatsArgs


def run_stats_spec(settings: Settings) -> ToolSpec:
    """构造当前运行统计工具声明。

    Args:
        settings: 工具超时与配额配置。

    Returns:
        只读工具声明。
    """

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """调用执行器已有聚合边界，保持真实数据库与权限校验。

        Args:
            context: 可信运行上下文。
            arguments: 已校验参数。

        Returns:
            当前用户和空间的运行聚合。
        """
        if not isinstance(arguments, RunStatsArgs):
            raise TypeError("运行统计参数类型不正确")
        result = await context.executor._stats(arguments)
        return result.model_dump()

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """只保存聚合字段，不保存任何消息正文。

        Args:
            arguments: 已校验参数。
            result: 聚合结果。

        Returns:
            脱敏审计摘要。
        """
        assert isinstance(arguments, RunStatsArgs)
        data = result if isinstance(result, dict) else {}
        return {"days": arguments.days, "total": data.get("total"), "counts": data.get("counts")}

    return ToolSpec(
        key=TOOL_NAME,
        version=1,
        display_name="我的运行统计",
        description="查询当前用户在当前空间最近 1–90 天的问答运行数量和状态分布，包含本次运行。",
        args_model=RunStatsArgs,
        risk="read",
        approval="never",
        timeout_seconds=settings.tool_timeout_seconds,
        quota_per_run=settings.tool_max_calls_per_run,
        handler=handler,
        audit=audit,
    )
