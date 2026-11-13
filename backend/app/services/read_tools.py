"""问答与任务共用的只读业务能力，不由模型提供身份。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.conversation import Conversation, Run
from app.schemas.auth import ResourceScope
from app.schemas.tools import RunStatsArgs, RunStatsResult
from app.services.auth import require_membership


async def query_run_stats(
    factory: async_sessionmaker[AsyncSession], scope: ResourceScope, arguments: RunStatsArgs
) -> RunStatsResult:
    """读取当前用户空间内的问答运行聚合，任务执行不计入问答 Run。

    Args:
        factory: 独立会话工厂。
        scope: 后台绑定的用户与空间。
        arguments: 严格校验后的天数。

    Returns:
        问答状态分布，不含消息正文。
    """
    async with factory() as session:
        await require_membership(session, scope, write=True)
        rows = (
            await session.execute(
                select(Run.status, func.count(Run.id))
                .join(Conversation)
                .where(
                    Conversation.created_by_user_id == scope.user_id,
                    Conversation.workspace_id == scope.workspace_id,
                    Run.created_at >= datetime.now(UTC) - timedelta(days=arguments.days),
                )
                .group_by(Run.status)
            )
        ).all()
        counts = {status: 0 for status in ("pending", "running", "completed", "failed", "cancelled")}
        counts.update({status: count for status, count in rows})
        return RunStatsResult(days=arguments.days, total=sum(counts.values()), counts=counts)
