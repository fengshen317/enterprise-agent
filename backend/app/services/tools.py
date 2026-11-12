"""原生工具执行边界；身份由运行归属绑定，模型不能选择身份。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capabilities.registry import ToolRegistry, build_default_registry
from app.capabilities.spec import AgentAssembly, ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.conversation import Conversation, Run
from app.models.tool_call import ToolCall
from app.schemas.auth import ResourceScope
from app.schemas.knowledge import KnowledgeSearchArgs, KnowledgeSearchResult
from app.schemas.tools import RunStatsArgs, RunStatsResult, ToolCallView
from app.services.agent_assembler import AgentAssembler
from app.services.auth import require_membership
from app.services.knowledge import KnowledgeService
from app.services.read_tools import query_run_stats

if TYPE_CHECKING:
    from app.services.approvals import ApprovalService
    from app.services.schedule_store import ScheduleStore

ACCESS_CODES = ("ACCESS_REVOKED", "WORKSPACE_ACCESS_DENIED", "WORKSPACE_ARCHIVED")


class ToolExecutor:
    """每次运行独享入口，事务不跨越外部能力调用。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        run_id: UUID,
        knowledge_service: KnowledgeService | None = None,
        *,
        assembly: AgentAssembly | None = None,
        registry: ToolRegistry | None = None,
        approval_service: ApprovalService | None = None,
        schedule_store: ScheduleStore | None = None,
    ) -> None:
        """绑定不可由模型覆盖的运行与能力装配。

        Args:
            factory: 独立数据库会话工厂。
            settings: 开关、超时和调用次数配置。
            run_id: 服务端创建的当前运行。
            knowledge_service: 可选知识库服务，未启用时为 None。
            assembly: 本次运行的能力快照；为空时使用内置默认 Agent。
            registry: 工具目录；为空时按当前依赖构造。
            approval_service: 写操作审批服务。
            schedule_store: 定时任务存储，供写工具使用。
        """
        self.factory, self.settings, self.run_id = factory, settings, run_id
        self.knowledge_service = knowledge_service
        self.registry = registry or build_default_registry(settings, knowledge_service)
        self.approval_service = approval_service
        self.schedule_store = schedule_store
        self.assembly = (
            assembly or AgentAssembler(self.factory, self.registry, settings, knowledge_service).default_assembly()
        )

    async def _scope_row(self, session: AsyncSession) -> tuple[ResourceScope, UUID]:
        """逐次复核运行、成员和空间，并返回对话标识。

        Args:
            session: 当前独立事务。

        Returns:
            可信范围与对话标识。

        Raises:
            ApplicationError: 运行已结束、被取消或权限失效。
        """
        row = (
            await session.execute(
                select(Conversation.id, Conversation.created_by_user_id, Conversation.workspace_id)
                .join(Run, Run.conversation_id == Conversation.id)
                .where(
                    Run.id == self.run_id,
                    Run.status == "running",
                    Run.cancel_requested_at.is_(None),
                    Conversation.status == "active",
                )
            )
        ).one_or_none()
        if row is None or row[1] is None or row[2] is None:
            raise ApplicationError("ACCESS_REVOKED", "当前运行已结束或无工具执行权限", 403)
        scope = ResourceScope(row[1], row[2])
        await require_membership(session, scope, write=True)
        return scope, row[0]

    async def _scope(self, session: AsyncSession) -> ResourceScope:
        """只返回可信范围，兼容现有权限测试。

        Args:
            session: 当前独立事务。

        Returns:
            可信用户和工作空间。
        """
        scope, _ = await self._scope_row(session)
        return scope

    async def _begin(self, name: str, limit: int, spec: ToolSpec | None = None) -> tuple[UUID, bool]:
        """锁定运行后按能力计数，防止并行调用绕过次数限制。

        Args:
            name: 已注册能力名。
            limit: 该能力配额。
            spec: 可选的工具声明，用于在审计行记录风险。

        Returns:
            审计 ID 和是否允许执行。
        """
        async with self.factory.begin() as session:
            await session.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
            await self._scope(session)
            count = await session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(ToolCall.run_id == self.run_id, ToolCall.tool_name == name)
            )
            identifier = uuid4()
            session.add(
                ToolCall(
                    id=identifier,
                    run_id=self.run_id,
                    tool_name=name,
                    tool_version=spec.version if spec is not None else None,
                    risk_level=spec.risk if spec is not None else None,
                    approval_required=spec.approval == "always" if spec is not None else False,
                )
            )
            return identifier, (count or 0) < limit

    async def _stats(self, arguments: RunStatsArgs) -> RunStatsResult:
        """查询当前用户与空间的运行聚合，不读取消息。

        Args:
            arguments: 已严格校验的查询条件。

        Returns:
            状态分布，包含本次运行。
        """
        async with self.factory() as session:
            scope = await self._scope(session)
        return await query_run_stats(self.factory, scope, arguments)

    async def _knowledge(self, arguments: KnowledgeSearchArgs) -> KnowledgeSearchResult:
        """按当前运行身份检索知识库，权限不接受模型参数。

        Args:
            arguments: 查询条件与可选知识库 ID。

        Returns:
            带应用侧来源的片段。

        Raises:
            ApplicationError: 知识库未启用。
        """
        if not self.settings.knowledge_enabled or self.knowledge_service is None:
            raise ApplicationError("KNOWLEDGE_DISABLED", "知识库功能未启用", 503)
        async with self.factory() as session:
            scope = await self._scope(session)
        return await self.knowledge_service.retrieve(scope, arguments)

    async def _finish(
        self,
        identifier: UUID,
        status: str,
        result: str | None,
        error_code: str | None,
        days: int | None = None,
        *,
        from_statuses: tuple[str, ...] = ("running", "awaiting_approval"),
    ) -> None:
        """只更新当前运行的审计，成功提交前再次检查权限。

        Args:
            identifier: 本次审计 ID。
            status: 终态。
            result: 脱敏审计摘要。
            error_code: 安全错误码。
            days: 旧运行统计字段。
            from_statuses: 允许迁移的起始状态。
        """
        async with self.factory.begin() as session:
            if status == "completed":
                await self._scope(session)
            await session.execute(
                update(ToolCall)
                .where(
                    ToolCall.id == identifier,
                    ToolCall.run_id == self.run_id,
                    ToolCall.status.in_(from_statuses),
                )
                .values(status=status, days=days, result=result, error_code=error_code, finished_at=datetime.now(UTC))
            )

    def _context(self, scope: ResourceScope, conversation_id: UUID) -> ToolContext:
        """构造单次调用的可信上下文。

        Args:
            scope: 已验证用户和空间。
            conversation_id: 当前对话。

        Returns:
            工具上下文。
        """
        return ToolContext(
            executor=self,
            factory=self.factory,
            settings=self.settings,
            run_id=self.run_id,
            conversation_id=conversation_id,
            scope=scope,
            knowledge_service=self.knowledge_service,
            schedule_store=self.schedule_store,
            assembly=self.assembly,
            skills={item.key: item for item in self.assembly.skills},
        )

    @staticmethod
    def _envelope(ok: bool, identifier: UUID, risk: str, data: object) -> str:
        """统一工具结果协议，模型只看到业务数据与审计标识。

        Args:
            ok: 是否成功。
            identifier: 审计 ID。
            risk: 风险级别。
            data: 业务结果。

        Returns:
            JSON 字符串。
        """
        return json.dumps(
            {"ok": ok, "audit_id": str(identifier), "risk": risk, "data": data},
            ensure_ascii=False,
            default=str,
        )

    async def invoke(self, name: str, raw_arguments: dict[str, object]) -> str:
        """统一注册表入口，返回有审计依据的 JSON 结果。

        Args:
            name: 稳定工具标识。
            raw_arguments: 不可信模型参数，不原样存储或回显。

        Returns:
            成功结果或明确失败状态。
        """
        if not self.settings.tools_enabled:
            raise ApplicationError("TOOL_NOT_ALLOWED", "当前工具未获授权", 403)
        spec = self.assembly.tool_map.get(name)
        if spec is None:
            raise ApplicationError("TOOL_NOT_ALLOWED", "当前工具未获授权", 403)
        # 系统工具按自身配额，业务工具再受全局单次运行上限约束。
        limit = spec.quota_per_run if spec.system else min(spec.quota_per_run, self.settings.tool_max_calls_per_run)
        identifier, allowed = await self._begin(name, limit, spec)
        arguments: BaseModel | None = None
        approval_approved = False
        try:
            if not allowed:
                raise ApplicationError("TOOL_CALL_LIMIT", "本次回答的工具调用次数已达上限", 422)
            arguments = spec.args_model.model_validate(raw_arguments)
            if spec.approval == "always":
                if self.approval_service is None:
                    raise ApplicationError("APPROVAL_UNAVAILABLE", "当前环境未启用写操作审批", 503)
                decision = await self.approval_service.request_and_wait(self, spec, arguments, identifier)
                if decision != "approved":
                    status = "denied" if decision == "rejected" else "expired" if decision == "expired" else "cancelled"
                    code = {
                        "denied": "APPROVAL_REJECTED",
                        "expired": "APPROVAL_EXPIRED",
                        "cancelled": "APPROVAL_CANCELLED",
                    }[status]
                    await self._finish(
                        identifier,
                        status,
                        None,
                        code,
                        from_statuses=("awaiting_approval",),
                    )
                    return json.dumps(
                        {
                            "ok": False,
                            "audit_id": str(identifier),
                            "risk": spec.risk,
                            "error_code": code,
                        },
                        ensure_ascii=False,
                    )
                approval_approved = True
            async with asyncio.timeout(spec.timeout_seconds):
                async with self.factory() as session:
                    scope, conversation_id = await self._scope_row(session)
                result = await spec.handler(self._context(scope, conversation_id), arguments)
            audit = spec.audit(arguments, result)
            raw_days = getattr(arguments, "days", None)
            days = raw_days if isinstance(raw_days, int) else None
            await self._finish(identifier, "completed", json.dumps(audit, ensure_ascii=False, default=str), None, days)
            if approval_approved and self.approval_service is not None:
                await self.approval_service.mark_executed(identifier)
            return self._envelope(True, identifier, spec.risk, result)
        except asyncio.CancelledError:
            if approval_approved and self.approval_service is not None:
                await self.approval_service.mark_failed(identifier)
            raw_days = getattr(arguments, "days", None)
            days = raw_days if isinstance(raw_days, int) else None
            await self._finish(identifier, "cancelled", None, "TOOL_CANCELLED", days)
            raise
        except Exception as exc:
            if approval_approved and self.approval_service is not None:
                await self.approval_service.mark_failed(identifier)
            if isinstance(exc, ValidationError):
                code = "TOOL_INVALID_ARGUMENTS"
            elif isinstance(exc, TimeoutError):
                code = "TOOL_TIMEOUT"
            elif isinstance(exc, ApplicationError):
                code = exc.code
            else:
                code = "TOOL_EXECUTION_FAILED"
            raw_days = getattr(arguments, "days", None)
            days = raw_days if isinstance(raw_days, int) else None
            await self._finish(
                identifier,
                "failed",
                None,
                code,
                days,
                from_statuses=("running", "awaiting_approval"),
            )
            if code in ACCESS_CODES:
                raise ApplicationError("ACCESS_REVOKED", "工具执行权限已失效", 403) from exc
            return json.dumps(
                {"ok": False, "audit_id": str(identifier), "risk": spec.risk, "error_code": code},
                ensure_ascii=False,
            )


async def list_tool_calls(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, scope: ResourceScope
) -> list[ToolCallView]:
    """按运行归属读取审计，不能通过列表访问其他人的结果。

    Args:
        factory: 独立数据库会话工厂。
        run_id: 目标运行。
        scope: 已验证用户和空间。

    Returns:
        按调用时间排列的安全记录。
    """
    async with factory() as session:
        await require_membership(session, scope)
        rows = await session.scalars(
            select(ToolCall)
            .join(Run)
            .join(Conversation)
            .where(
                Run.id == run_id,
                Conversation.workspace_id == scope.workspace_id,
                Conversation.created_by_user_id == scope.user_id,
            )
            .order_by(ToolCall.created_at, ToolCall.id)
        )
        return [ToolCallView.model_validate(row) for row in rows]
