"""写操作审批：单进程等待、数据库决策与审计事件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.approval import Approval, ApprovalEvent
from app.models.conversation import Conversation, Run
from app.models.tool_call import ToolCall
from app.schemas.approval import ApprovalView
from app.schemas.auth import ResourceScope
from app.services.auth import require_membership

if TYPE_CHECKING:
    from pydantic import BaseModel

    from app.capabilities.spec import ToolSpec
    from app.services.tools import ToolExecutor

ACTIVE = ("pending", "running")
TERMINAL = {"approved", "rejected", "expired", "cancelled", "executed", "failed"}


class ApprovalBroker:
    """单进程事件表；服务重启后只承诺收敛，不恢复等待。"""

    def __init__(self) -> None:
        """创建空事件表。"""
        self.events: dict[UUID, asyncio.Event] = {}

    def register(self, approval_id: UUID) -> asyncio.Event:
        """在插入审批前注册事件，避免决策早于等待的竞态。

        Args:
            approval_id: 审批标识。

        Returns:
            等待事件。
        """
        event = asyncio.Event()
        self.events[approval_id] = event
        return event

    def resolve(self, approval_id: UUID) -> None:
        """唤醒正在等待的审批。

        Args:
            approval_id: 审批标识。
        """
        event = self.events.get(approval_id)
        if event is not None:
            event.set()

    def unregister(self, approval_id: UUID) -> None:
        """清理已结束的等待。

        Args:
            approval_id: 审批标识。
        """
        self.events.pop(approval_id, None)


class ApprovalService:
    """创建审批、等待决策并维护审批链。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        """保存会话工厂、超时配置与事件表。

        Args:
            factory: 独立数据库会话工厂。
            settings: 审批超时配置。
        """
        self.factory = factory
        self.settings = settings
        self.broker = ApprovalBroker()

    async def request_and_wait(
        self,
        executor: ToolExecutor,
        spec: ToolSpec,
        arguments: BaseModel,
        tool_call_id: UUID,
    ) -> str:
        """创建审批并等待用户决策；超时或取消都会收敛。

        Args:
            executor: 当前工具执行器。
            spec: 写工具声明。
            arguments: 已校验参数。
            tool_call_id: 关联工具审计。

        Returns:
            pending 之外的审批状态。
        """
        approval_id = uuid4()
        event = self.broker.register(approval_id)
        conversation_id: UUID | None = None
        scope: ResourceScope | None = None
        try:
            async with executor.factory() as session:
                scope, conversation_id = await executor._scope_row(session)
            context = executor._context(scope, conversation_id)
            preview = (
                spec.approval_preview(arguments, context)
                if spec.approval_preview is not None
                else {"summary": f"{spec.display_name}需要执行"}
            )
            payload = json.dumps(arguments.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, default=str)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.approval_timeout_seconds)
            async with self.factory.begin() as session:
                await executor._scope(session)
                session.add(
                    Approval(
                        id=approval_id,
                        workspace_id=scope.workspace_id,
                        run_id=executor.run_id,
                        tool_call_id=tool_call_id,
                        tool_name=spec.key,
                        tool_version=spec.version,
                        risk_level=spec.risk,
                        args_digest=digest,
                        args_preview=preview,
                        requested_by_user_id=scope.user_id,
                        expires_at=expires_at,
                    )
                )
                session.add(
                    ApprovalEvent(
                        approval_id=approval_id,
                        action="requested",
                        actor_user_id=scope.user_id,
                        actor_role="system",
                        detail={"tool": spec.key},
                    )
                )
                await session.execute(
                    update(ToolCall)
                    .where(
                        ToolCall.id == tool_call_id,
                        ToolCall.run_id == executor.run_id,
                        ToolCall.status == "running",
                    )
                    .values(
                        status="awaiting_approval",
                        tool_version=spec.version,
                        risk_level=spec.risk,
                        approval_required=True,
                        arguments_digest=digest,
                    )
                )
            deadline = monotonic() + self.settings.approval_timeout_seconds
            while True:
                status = await self._status(approval_id)
                if status is not None and status != "pending":
                    return status
                remaining = deadline - monotonic()
                if remaining <= 0:
                    await self._set_status(approval_id, "expired")
                    return "expired"
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(1.0, remaining))
                except TimeoutError:
                    continue
                event.clear()
        except asyncio.CancelledError:
            await self._set_status(approval_id, "cancelled")
            raise
        finally:
            self.broker.unregister(approval_id)

    async def decide(
        self,
        scope: ResourceScope,
        run_id: UUID,
        approval_id: UUID,
        decision: str,
        comment: str | None,
    ) -> ApprovalView:
        """只允许运行所有者批准或拒绝，重复决策幂等。

        Args:
            scope: 当前用户和空间。
            run_id: 目标运行。
            approval_id: 审批标识。
            decision: approve 或 reject。
            comment: 可选说明。

        Returns:
            审批视图。

        Raises:
            ApplicationError: 审批不存在、非所有者或运行已结束。
        """
        if decision not in {"approve", "reject"}:
            raise ApplicationError("APPROVAL_INVALID_DECISION", "无效的审批决定", 422)
        async with self.factory.begin() as session:
            await require_membership(session, scope)
            approval = await session.scalar(
                select(Approval).where(Approval.id == approval_id, Approval.run_id == run_id).with_for_update()
            )
            if approval is None:
                raise ApplicationError("APPROVAL_NOT_FOUND", "审批不存在", 404)
            run_status = await session.scalar(
                select(Run.status)
                .join(Conversation, Conversation.id == Run.conversation_id)
                .where(
                    Run.id == run_id,
                    Conversation.workspace_id == scope.workspace_id,
                    Conversation.created_by_user_id == scope.user_id,
                )
            )
            if run_status is None:
                raise ApplicationError("APPROVAL_NOT_FOUND", "审批不存在", 404)
            if approval.status != "pending":
                return self._view(approval)
            if run_status not in ACTIVE:
                raise ApplicationError("RUN_NOT_ACTIVE", "运行已结束，不能再审批", 409)
            now = datetime.now(UTC)
            if now >= approval.expires_at:
                approval.status = "expired"
                approval.updated_at = now
                session.add(ApprovalEvent(approval_id=approval.id, action="expired", actor_role="system"))
                self.broker.resolve(approval.id)
                return self._view(approval)
            approval.status = "approved" if decision == "approve" else "rejected"
            approval.decided_by_user_id = scope.user_id
            approval.decided_at = now
            approval.decision_comment = comment
            approval.updated_at = now
            session.add(
                ApprovalEvent(
                    approval_id=approval.id,
                    action=approval.status,
                    actor_user_id=scope.user_id,
                    actor_role="user",
                    detail={"comment": comment or ""},
                )
            )
            self.broker.resolve(approval.id)
            return self._view(approval)

    async def list_for_run(self, scope: ResourceScope, run_id: UUID) -> list[ApprovalView]:
        """读取一次运行的全部审批链。

        Args:
            scope: 当前用户和空间。
            run_id: 目标运行。

        Returns:
            按请求时间排列的审批。
        """
        async with self.factory() as session:
            await require_membership(session, scope)
            owned = await session.scalar(
                select(Run.id)
                .join(Conversation, Conversation.id == Run.conversation_id)
                .where(
                    Run.id == run_id,
                    Conversation.workspace_id == scope.workspace_id,
                    Conversation.created_by_user_id == scope.user_id,
                )
            )
            if owned is None:
                raise ApplicationError("RUN_NOT_FOUND", "运行不存在", 404)
            rows = list(
                await session.scalars(
                    select(Approval).where(Approval.run_id == run_id).order_by(Approval.requested_at, Approval.id)
                )
            )
            return [self._view(row) for row in rows]

    async def mark_executed(self, tool_call_id: UUID) -> None:
        """标记审批已执行；找不到审批时静默忽略。

        Args:
            tool_call_id: 关联工具审计。
        """
        await self._mark(tool_call_id, "executed")

    async def mark_failed(self, tool_call_id: UUID) -> None:
        """标记审批执行失败。

        Args:
            tool_call_id: 关联工具审计。
        """
        await self._mark(tool_call_id, "failed")

    async def _mark(self, tool_call_id: UUID, status: str) -> None:
        """更新审批终态并追加事件。

        Args:
            tool_call_id: 关联工具审计。
            status: executed 或 failed。
        """
        async with self.factory.begin() as session:
            approval = await session.scalar(
                select(Approval).where(Approval.tool_call_id == tool_call_id).with_for_update()
            )
            if approval is None or approval.status != "approved":
                return
            now = datetime.now(UTC)
            approval.status = status
            approval.executed_at = now
            approval.updated_at = now
            session.add(ApprovalEvent(approval_id=approval.id, action=status, actor_role="system"))

    async def _status(self, approval_id: UUID) -> str | None:
        """读取当前审批状态。

        Args:
            approval_id: 审批标识。

        Returns:
            状态或空。
        """
        async with self.factory() as session:
            value = await session.scalar(select(Approval.status).where(Approval.id == approval_id))
            return value if isinstance(value, str) else None

    async def _set_status(self, approval_id: UUID, status: str) -> None:
        """将 pending 审批收敛为过期或取消。

        Args:
            approval_id: 审批标识。
            status: expired 或 cancelled。
        """
        async with self.factory.begin() as session:
            approval = await session.scalar(
                select(Approval).where(Approval.id == approval_id, Approval.status == "pending").with_for_update()
            )
            if approval is None:
                return
            now = datetime.now(UTC)
            approval.status = status
            approval.updated_at = now
            session.add(ApprovalEvent(approval_id=approval.id, action=status, actor_role="system"))
            self.broker.resolve(approval.id)

    @staticmethod
    def _view(approval: Approval) -> ApprovalView:
        """转换为前端可见的审批视图。

        Args:
            approval: 审批记录。

        Returns:
            审批视图。
        """
        return ApprovalView(
            id=approval.id,
            run_id=approval.run_id,
            tool_call_id=approval.tool_call_id,
            tool_name=approval.tool_name,
            tool_version=approval.tool_version,
            risk_level=approval.risk_level,  # type: ignore[arg-type]
            args_preview=approval.args_preview,
            status=approval.status,  # type: ignore[arg-type]
            requested_at=approval.requested_at,
            expires_at=approval.expires_at,
            decided_at=approval.decided_at,
            decision_comment=approval.decision_comment,
        )
