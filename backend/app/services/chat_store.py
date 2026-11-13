"""问答事务边界；每次调用独占一个短生命周期数据库会话。"""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.context_types import ContextMessage, ContextState
from app.core.errors import ApplicationError
from app.models.approval import Approval, ApprovalEvent
from app.models.conversation import Conversation, Message, Run
from app.models.identity import User, Workspace, WorkspaceMembership
from app.models.tool_call import ToolCall
from app.schemas.approval import ApprovalView
from app.schemas.auth import ResourceScope
from app.schemas.chat import ConversationView, MessagePage, MessageView, RunCreate, RunSnapshot, RunView
from app.services.auth import owned_conversations, require_membership

ACTIVE = ("pending", "running")


class ChatStore:
    """事务只覆盖业务数据，不跨越模型调用或 SSE 等待。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """注入会话工厂。

        Args:
            factory: 当前应用的独立会话工厂。
        """
        self.factory = factory

    async def create_conversation(
        self, title: str, scope: ResourceScope, agent_profile_id: UUID | None = None
    ) -> ConversationView:
        """创建对话。

        Args:
            title: 已校验标题。
            scope: 可信用户与工作空间。
            agent_profile_id: 可选绑定的 Agent。

        Returns:
            持久化对话。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            conversation = Conversation(
                title=title,
                workspace_id=scope.workspace_id,
                created_by_user_id=scope.user_id,
                agent_profile_id=agent_profile_id,
            )
            session.add(conversation)
            await session.flush()
            return ConversationView.model_validate(conversation)

    async def list_conversations(self, offset: int, limit: int, scope: ResourceScope) -> list[ConversationView]:
        """按稳定顺序列出对话。

        Args:
            offset: 跳过数量。
            scope: 当前用户与空间。
            limit: 页面大小。

        Returns:
            对话列表。
        """
        async with self.factory() as session:
            await require_membership(session, scope)
            items = await session.scalars(
                owned_conversations(scope)
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [ConversationView.model_validate(item) for item in items]

    async def messages(self, conversation_id: UUID, after: int, limit: int) -> MessagePage:
        """分页读取历史，附上可恢复订阅的运行标识。

        Args:
            conversation_id: 对话标识。
            after: 排除此序号及以前消息。
            limit: 页面大小。

        Returns:
            递增消息页。
        """
        async with self.factory() as session:
            if await session.get(Conversation, conversation_id) is None:
                raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)
            rows = list(
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id, Message.sequence > after)
                    .order_by(Message.sequence)
                    .limit(limit + 1)
                )
            )
            active = await session.scalar(
                select(Run.id).where(Run.conversation_id == conversation_id, Run.status.in_(ACTIVE))
            )
            return MessagePage(
                items=[MessageView.model_validate(row) for row in rows[:limit]],
                has_more=len(rows) > limit,
                active_run_id=active,
            )

    async def create_run(
        self,
        conversation_id: UUID,
        body: RunCreate,
        model: str,
        scope: ResourceScope,
        *,
        agent_profile_id: UUID | None = None,
        agent_snapshot: dict[str, object] | None = None,
    ) -> RunSnapshot:
        """锁定对话，原子分配序号并创建输入、运行和回复占位。

        Args:
            conversation_id: 对话标识。
            body: 输入内容和幂等键。
            model: 实际模型标识。
            scope: 服务端身份范围。
            agent_profile_id: 提交时绑定的 Agent。
            agent_snapshot: 提交时固定的能力快照。

        Returns:
            新运行或同键同内容的已有运行。
        """
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            conversation = await session.scalar(
                owned_conversations(scope).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)
            existing = await session.scalar(
                select(Run).where(Run.conversation_id == conversation_id, Run.idempotency_key == body.idempotency_key)
            )
            if existing is not None:
                original = await session.scalar(
                    select(Message.content).where(
                        Message.conversation_id == conversation_id, Message.sequence == existing.input_sequence
                    )
                )
                if original != body.content:
                    raise ApplicationError("IDEMPOTENCY_CONFLICT", "同一幂等键不能提交不同问题", 409)
                return await self._snapshot(session, existing)
            if conversation.status != "active":
                raise ApplicationError("CONVERSATION_ARCHIVED", "归档对话不能继续生成", 409)
            if (
                await session.scalar(
                    select(Run.id).where(Run.conversation_id == conversation_id, Run.status.in_(ACTIVE))
                )
                is not None
            ):
                raise ApplicationError("RUN_IN_PROGRESS", "当前对话正在生成，请等待或停止后重试", 409)
            sequence = conversation.next_message_sequence
            run = Run(
                id=uuid4(),
                conversation_id=conversation_id,
                idempotency_key=body.idempotency_key,
                input_sequence=sequence,
                model=model,
                agent_profile_id=agent_profile_id,
                agent_snapshot=agent_snapshot or {},
            )
            session.add(run)
            # 显式 flush 避免没有 ORM relationship 时插入顺序依赖偶然的工作单元排序。
            await session.flush()
            session.add_all(
                [
                    Message(
                        conversation_id=conversation_id,
                        sequence=sequence,
                        role="user",
                        status="completed",
                        content=body.content,
                    ),
                    Message(
                        conversation_id=conversation_id,
                        run_id=run.id,
                        sequence=sequence + 1,
                        role="assistant",
                        status="streaming",
                        content="",
                    ),
                ]
            )
            conversation.next_message_sequence += 2
            conversation.updated_at = datetime.now(UTC)
            if sequence == 1 and conversation.title == "新对话":
                conversation.title = body.content.strip()[:40]
            await session.flush()
            return await self._snapshot(session, run)

    async def _snapshot(self, session: AsyncSession, run: Run) -> RunSnapshot:
        """在已有事务中查询运行的唯一助手回复。

        Args:
            session: 当前独占会话。
            run: 已加载运行。

        Returns:
            状态和内容的快照。
        """
        message = (await session.scalars(select(Message).where(Message.run_id == run.id))).one()
        approval = await session.scalar(
            select(Approval)
            .where(Approval.run_id == run.id, Approval.status == "pending")
            .order_by(Approval.requested_at.desc(), Approval.id.desc())
            .limit(1)
        )
        return RunSnapshot(
            run=RunView.model_validate(run),
            message=MessageView.model_validate(message),
            approval=self._approval_view(approval) if approval is not None else None,
        )

    @staticmethod
    def _approval_view(approval: Approval) -> ApprovalView:
        """把待审批记录转换为快照字段。

        Args:
            approval: 审批记录。

        Returns:
            公开审批视图。
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

    async def snapshot(self, run_id: UUID) -> RunSnapshot:
        """查询运行并校验存在性。

        Args:
            run_id: 运行标识。

        Returns:
            最新快照。
        """
        async with self.factory() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise ApplicationError("RUN_NOT_FOUND", "运行不存在", 404)
            return await self._snapshot(session, run)

    async def run_authorized(self, run_id: UUID) -> bool:
        """执行器持续复核资源权限，不借用前端会话延长权限。

        Args:
            run_id: 正在生成的运行。

        Returns:
            账号、成员和空间是否仍允许生成。
        """
        async with self.factory() as session:
            found = await session.scalar(
                select(Run.id)
                .join(Conversation)
                .join(
                    WorkspaceMembership,
                    and_(
                        WorkspaceMembership.workspace_id == Conversation.workspace_id,
                        WorkspaceMembership.user_id == Conversation.created_by_user_id,
                    ),
                )
                .join(User, User.id == Conversation.created_by_user_id)
                .join(Workspace, Workspace.id == Conversation.workspace_id)
                .where(
                    Run.id == run_id,
                    User.status == "active",
                    WorkspaceMembership.status == "active",
                    Workspace.status == "active",
                    Conversation.status == "active",
                )
            )
            return found is not None

    async def run_scope(self, run_id: UUID) -> ResourceScope | None:
        """从运行反查可信用户和空间，供自动检索使用。

        Args:
            run_id: 运行标识。

        Returns:
            可信范围，运行不存在或归属缺失时为空。
        """
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(Conversation.created_by_user_id, Conversation.workspace_id)
                    .join(Run, Run.conversation_id == Conversation.id)
                    .where(Run.id == run_id, Conversation.status == "active")
                )
            ).one_or_none()
            if row is None or row[0] is None or row[1] is None:
                return None
            return ResourceScope(row[0], row[1])

    async def record_auto_retrieval(
        self, run_id: UUID, query: str, chunks: list[dict[str, object]], error_code: str | None = None
    ) -> None:
        """记录自动知识库检索审计，不占工具配额。

        Args:
            run_id: 当前运行。
            query: 检索问题（已截断）。
            chunks: 命中的 chunk 摘要。
            error_code: 失败时的错误码。
        """
        from datetime import UTC, datetime
        from uuid import uuid4

        async with self.factory.begin() as session:
            now = datetime.now(UTC)
            status = "completed" if error_code is None else "failed"
            session.add(
                ToolCall(
                    id=uuid4(),
                    run_id=run_id,
                    tool_name="auto_knowledge_retrieval",
                    status=status,
                    result=json.dumps({"query": query, "chunks": chunks}, ensure_ascii=False),
                    error_code=error_code,
                    finished_at=now,
                )
            )

    async def begin_run(self, run_id: UUID) -> ContextState | None:
        """领取运行并从业务消息重建上下文，跳过失败回复及其输入。

        Args:
            run_id: 运行标识。

        Returns:
            已领取运行的摘要和未摘要消息，或无需执行时返回空。
        """
        async with self.factory.begin() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status != "pending":
                return None
            run.status = "running"
            run.started_at = datetime.now(UTC)
            conversation = (
                await session.scalars(select(Conversation).where(Conversation.id == run.conversation_id))
            ).one()
            rows = list(
                await session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == run.conversation_id,
                        Message.sequence <= run.input_sequence,
                        Message.sequence > conversation.summary_through_sequence,
                    )
                    .order_by(Message.sequence)
                )
            )
            successful_inputs = set(
                await session.scalars(
                    select(Run.input_sequence).where(
                        Run.conversation_id == run.conversation_id,
                        Run.status == "completed",
                        Run.input_sequence > conversation.summary_through_sequence,
                    )
                )
            )
            history = [
                ContextMessage(row.sequence, row.role, row.content)
                for row in rows
                if (row.role == "assistant" and row.status == "completed")
                or (row.role == "user" and (row.sequence in successful_inputs or row.sequence == run.input_sequence))
            ]
            return ContextState(
                run_id,
                conversation.summary,
                conversation.summary_through_sequence,
                conversation.summary_version,
                history,
            )

    async def save_summary(
        self, run_id: UUID, expected_version: int, through_sequence: int, summary: str, model: str
    ) -> bool:
        """原子更新摘要与游标，防止取消后提交或覆盖新版本。

        Args:
            run_id: 正在执行压缩的运行。
            expected_version: 构建上下文时读取的版本。
            through_sequence: 最后纳入摘要的助手消息序号。
            summary: 已校验的摘要。
            model: 摘要模型标识。

        Returns:
            是否成功提交；已取消或终止时返回 False。
        """
        async with self.factory.begin() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status != "running" or run.cancel_requested_at is not None:
                return False
            conversation = (
                await session.scalars(
                    select(Conversation).where(Conversation.id == run.conversation_id).with_for_update()
                )
            ).one()
            if conversation.summary_version != expected_version:
                raise ApplicationError("SUMMARY_STATE_CHANGED", "对话摘要状态已变化，请重试", 409)
            boundary = await session.scalar(
                select(Message.id)
                .join(Run, Message.run_id == Run.id)
                .where(
                    Message.conversation_id == run.conversation_id,
                    Message.sequence == through_sequence,
                    Message.role == "assistant",
                    Message.status == "completed",
                    Run.status == "completed",
                )
            )
            if (
                boundary is None
                or through_sequence % 2 != 0
                or not summary.strip()
                or through_sequence <= conversation.summary_through_sequence
                or through_sequence >= run.input_sequence
            ):
                raise ApplicationError("INVALID_SUMMARY_BOUNDARY", "对话摘要范围无效", 409)
            conversation.summary = summary
            conversation.summary_through_sequence = through_sequence
            conversation.summary_version += 1
            conversation.summary_updated_at = datetime.now(UTC)
            conversation.summary_model = model
            return True

    async def agent_context(self, run_id: UUID) -> str:
        """携带同一对话最近一轮已完成的协作资料，保留停止后补充要求的上下文。

        Args:
            run_id: 已领取且通过运行权限校验的当前运行。

        Returns:
            明确标记为资料的有限上下文；普通问答不额外注入。
        """
        async with self.factory() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return ""
            previous = await session.scalar(
                select(Run)
                .where(Run.conversation_id == run.conversation_id, Run.input_sequence < run.input_sequence)
                .order_by(Run.input_sequence.desc())
                .limit(1)
            )
            if previous is None:
                return ""
            records = list(
                await session.scalars(
                    select(ToolCall)
                    .where(
                        ToolCall.run_id == previous.id,
                        ToolCall.status == "completed",
                        ToolCall.tool_name.in_(("update_plan", "delegate_agent")),
                    )
                    .order_by(ToolCall.created_at, ToolCall.id)
                )
            )
            if not records:
                return ""
            goal = await session.scalar(
                select(Message.content).where(
                    Message.conversation_id == run.conversation_id, Message.sequence == previous.input_sequence
                )
            )
            # 只取公开计划和完整成功子任务的有限资料；中断正文不会被当作可靠结果复用。
            latest_plan = next((row for row in reversed(records) if row.tool_name == "update_plan"), None)
            selected = ([latest_plan] if latest_plan else []) + [
                row for row in records if row.tool_name == "delegate_agent"
            ][-2:]
            parts = ["上一轮协作目标：" + (goal or ""), "上一轮状态：" + previous.status]
            for record in selected:
                text = record.result or ""
                parts.append(
                    record.tool_name + "：" + text[:6000] + ("（资料过长，已截取）" if len(text) > 6000 else "")
                )
            return "\n".join(parts)

    async def save_content(self, run_id: UUID, content: str) -> None:
        """批量覆盖累计文本，避免逐字符插入消息。

        Args:
            run_id: 运行标识。
            content: 本次已收到的完整正文。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(Message)
                .where(Message.run_id == run_id, Message.status == "streaming")
                .values(content=content, updated_at=datetime.now(UTC))
            )

    async def finish(
        self, run_id: UUID, content: str, status: str, code: str | None = None, error: str | None = None
    ) -> None:
        """以锁保护终态，取消请求先提交时优先收敛为取消。

        Args:
            run_id: 运行标识。
            content: 已收到的正文。
            status: 目标终态。
            code: 安全错误码。
            error: 安全错误说明。
        """
        async with self.factory.begin() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status not in ACTIVE:
                return
            if run.cancel_requested_at is not None:
                status, code, error = "cancelled", None, None
            now = datetime.now(UTC)
            run.status, run.finished_at = status, now
            run.error_code, run.error_message = code, error
            await session.execute(
                update(Message)
                .where(Message.run_id == run_id)
                .values(content=content, status="completed" if status == "completed" else "interrupted", updated_at=now)
            )
            await session.execute(
                update(Conversation).where(Conversation.id == run.conversation_id).values(updated_at=now)
            )
            pending = list(
                await session.scalars(
                    select(Approval).where(Approval.run_id == run_id, Approval.status == "pending").with_for_update()
                )
            )
            for approval in pending:
                approval.status = "cancelled"
                approval.updated_at = now
                session.add(ApprovalEvent(approval_id=approval.id, action="cancelled", actor_role="system"))
            await session.execute(
                update(ToolCall)
                .where(ToolCall.run_id == run_id, ToolCall.status.in_(("running", "awaiting_approval")))
                .values(
                    status="cancelled" if status == "cancelled" else "failed",
                    error_code=code or "TOOL_CANCELLED",
                    finished_at=now,
                )
            )

    async def cancel(self, run_id: UUID) -> RunSnapshot:
        """幂等记录停止请求，实际终态由执行器提交。

        Args:
            run_id: 运行标识。

        Returns:
            请求后的运行快照。
        """
        async with self.factory.begin() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None:
                raise ApplicationError("RUN_NOT_FOUND", "运行不存在", 404)
            if run.status in ACTIVE and run.cancel_requested_at is None:
                run.cancel_requested_at = datetime.now(UTC)
            return await self._snapshot(session, run)

    async def recover(self) -> None:
        """本机单执行进程获得数据库锁后收敛上次遗留运行。"""
        async with self.factory.begin() as session:
            ids = list(await session.scalars(select(Run.id).where(Run.status.in_(ACTIVE))))
            if not ids:
                return
            now = datetime.now(UTC)
            await session.execute(
                update(ToolCall)
                .where(ToolCall.status.in_(("running", "awaiting_approval")))
                .values(status="failed", error_code="PROCESS_RESTARTED", finished_at=now)
            )
            pending = list(
                await session.scalars(select(Approval).where(Approval.status == "pending").with_for_update())
            )
            for approval in pending:
                approval.status = "expired"
                approval.updated_at = now
                session.add(ApprovalEvent(approval_id=approval.id, action="expired", actor_role="system"))
            await session.execute(
                update(Run)
                .where(Run.id.in_(ids))
                .values(
                    status="failed",
                    finished_at=now,
                    error_code="PROCESS_RESTARTED",
                    error_message="服务重启中断了生成，请重新提交问题",
                )
            )
            await session.execute(
                update(Message).where(Message.run_id.in_(ids)).values(status="interrupted", updated_at=now)
            )
