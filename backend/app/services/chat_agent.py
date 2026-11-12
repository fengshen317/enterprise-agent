"""对话内规划与单层子 Agent 编排，不跨模型请求持有数据库事务。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capabilities.spec import AgentAssembly, ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.tool_call import ToolCall
from app.schemas.chat_agent import CHILD_TOOL, DELEGATE_TOOL, PLAN_TOOL, DelegateArgs, DelegatedTask, PlanArgs
from app.services.knowledge import KnowledgeService
from app.services.task_retry import classify_error, retry_delay
from app.services.tools import ToolExecutor

if TYPE_CHECKING:
    from app.capabilities.registry import ToolRegistry
    from app.services.approvals import ApprovalService
    from app.services.schedule_store import ScheduleStore

T = TypeVar("T")
MAX_PLAN_UPDATES = 12
MAX_DELEGATIONS = 6
MAX_MODEL_CALLS = 30


class ChatAgentExecutor(ToolExecutor):
    """所有子任务共享运行身份和配额，独享 Agent 图与上下文。"""

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
        """绑定运行身份、共享并发门限与本次能力装配。

        Args:
            factory: 独立会话工厂。
            settings: 当前配置。
            run_id: 可信运行标识。
            knowledge_service: 可选知识库服务，主子 Agent 共用。
            assembly: 业务能力装配；为空时使用内置默认 Agent。
            registry: 工具目录。
            approval_service: 写操作审批服务。
            schedule_store: 定时任务存储。
        """
        super().__init__(
            factory,
            settings,
            run_id,
            knowledge_service,
            assembly=assembly,
            registry=registry,
            approval_service=approval_service,
            schedule_store=schedule_store,
        )
        system_specs: list[ToolSpec] = []
        if self.assembly.allow_planning:
            system_specs.append(self._plan_spec())
        if self.assembly.allow_delegation:
            system_specs.append(self._delegate_spec())
        if any(skill.activation == "on_demand" for skill in self.assembly.skills):
            from app.capabilities.builtins.skills import load_skill_spec

            system_specs.append(load_skill_spec(self.settings))
        if system_specs:
            self.assembly = replace(self.assembly, tools=self.assembly.tools + tuple(system_specs))
        self.child_slots = asyncio.Semaphore(2)

    def _plan_spec(self) -> ToolSpec:
        """构造主 Agent 的公开计划工具。

        Returns:
            系统工具声明。
        """
        return ToolSpec(
            key=PLAN_TOOL,
            version=1,
            display_name="更新工作计划",
            description="创建或更新最多8步的公开工作计划，状态 pending/in_progress/completed。仅复杂请求使用。",
            args_model=PlanArgs,
            risk="read",
            approval="never",
            timeout_seconds=2,
            quota_per_run=MAX_PLAN_UPDATES,
            handler=self._handle_plan,
            audit=lambda arguments, result: arguments.model_dump(),
            child_allowed=False,
            context_eligible=True,
            system=True,
        )

    def _delegate_spec(self) -> ToolSpec:
        """构造主 Agent 的并行委派工具。

        Returns:
            系统工具声明。
        """
        return ToolSpec(
            key=DELEGATE_TOOL,
            version=1,
            display_name="委派子 Agent",
            description=(
                "并行委派1到2个独立子任务。role为analyst或reviewer；"
                "instruction必须含所需资料。子 Agent 无法联网或执行业务写入。"
            ),
            args_model=DelegateArgs,
            risk="read",
            approval="never",
            timeout_seconds=float(self.settings.chat_run_timeout_seconds),
            quota_per_run=MAX_DELEGATIONS,
            handler=self._handle_delegate,
            audit=lambda arguments, result: result if isinstance(result, dict) else {},
            child_allowed=False,
            system=True,
        )

    async def _handle_plan(self, context: ToolContext, arguments: BaseModel) -> object:
        """保存公开计划；参数已由执行入口严格校验。

        Args:
            context: 可信运行上下文。
            arguments: 计划参数。

        Returns:
            计划数据。
        """
        if not isinstance(arguments, PlanArgs):
            raise TypeError("计划参数类型不正确")
        return arguments.model_dump()

    async def _handle_delegate(self, context: ToolContext, arguments: BaseModel) -> object:
        """并行执行独立子任务，任一失败都不包装为整体成功。

        Args:
            context: 可信运行上下文。
            arguments: 委派参数。

        Returns:
            子任务真实结果。

        Raises:
            ApplicationError: 有子任务未完成。
        """
        if not isinstance(arguments, DelegateArgs):
            raise TypeError("委派参数类型不正确")
        # TaskGroup 保证取消/撤权会等待所有分支退出，避免留下孤立子任务。
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._child(task)) for task in arguments.tasks]
        results = [task.result() for task in tasks]
        if not all(result.get("ok") is True for result in results):
            raise ApplicationError("SUBAGENT_FAILED", "部分子任务未完成", 422)
        return {"results": results}

    async def _child(self, task: DelegatedTask) -> dict[str, object]:
        """限制同一运行的所有并发委派批次最多执行两个子 Agent。

        Args:
            task: 已校验子任务。

        Returns:
            子任务真实结果。
        """
        async with self.child_slots:
            return await self._run_child(task)

    async def progress(self, identifier: UUID, data: dict[str, object]) -> None:
        """保存公开进度并复核权限，避免等待期间泄露撤权后的资料。

        Args:
            identifier: 当前运行的审计记录。
            data: 经业务构造的公开状态。
        """
        async with self.factory.begin() as session:
            await self._scope(session)
            await session.execute(
                update(ToolCall)
                .where(ToolCall.id == identifier, ToolCall.run_id == self.run_id, ToolCall.status == "running")
                .values(result=json.dumps(data, ensure_ascii=False))
            )

    async def model_call(self, call: Callable[[], Awaitable[T]]) -> T:
        """只重试当前模型动作，不重新执行图中已完成的工具。

        Args:
            call: 当前模型请求。

        Returns:
            完整成功响应，失败尝试正文不会发布。
        """
        identifier, allowed = await self._begin("model_request", MAX_MODEL_CALLS)
        try:
            if not allowed:
                raise ApplicationError("MODEL_CALL_LIMIT", "本次对话的模型调用次数已达上限", 422)
            for attempt in range(1, self.settings.task_retry_attempts + 1):
                await self.progress(identifier, {"attempt": attempt, "max_attempts": self.settings.task_retry_attempts})
                try:
                    value = await call()
                    await self._finish(
                        identifier,
                        "completed",
                        json.dumps({"attempt": attempt, "max_attempts": self.settings.task_retry_attempts}),
                        None,
                    )
                    return value
                except Exception as exc:
                    decision = classify_error(exc)
                    if not decision.retryable or attempt == self.settings.task_retry_attempts:
                        raise ApplicationError(decision.code, "模型请求失败，请稍后在当前对话重试", 502) from exc
                    delay = retry_delay(
                        decision, attempt, self.settings.task_retry_base_seconds, self.settings.task_retry_max_seconds
                    )
                    if delay > self.settings.task_retry_max_wait_seconds:
                        raise ApplicationError("RETRY_WAIT_TOO_LONG", "服务要求较长等待，请稍后重试", 503) from exc
                    await self.progress(
                        identifier,
                        {
                            "attempt": attempt,
                            "max_attempts": self.settings.task_retry_attempts,
                            "error_code": decision.code,
                            "next_retry_at": (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                        },
                    )
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            await self._finish(identifier, "cancelled", None, "MODEL_CANCELLED")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ApplicationError) else "MODEL_CALL_FAILED"
            await self._finish(identifier, "failed", None, code)
            raise
        raise ApplicationError("MODEL_CALL_FAILED", "模型未返回结果", 502)

    async def _run_child(self, task: DelegatedTask) -> dict[str, object]:
        """启动真实独立图，禁止子 Agent 再规划或委派。

        Args:
            task: 主 Agent 交付的任务与资料。

        Returns:
            有真实执行状态的结果，失败不会被包装为成功。
        """
        from app.agents.provider import DeepSeekProvider

        identifier, allowed = await self._begin(CHILD_TOOL, MAX_DELEGATIONS)
        public: dict[str, object] = {"role": task.role, "title": task.title}
        try:
            if not allowed:
                raise ApplicationError("DELEGATION_LIMIT", "本次对话的子任务数量已达上限", 422)
            await self.progress(identifier, public)
            provider = DeepSeekProvider(self.settings, self, assembly=self.assembly.for_child(), child_role=task.role)
            output = ""
            async for fragment in provider.stream([("user", task.instruction)]):
                output += fragment
                if len(output) > self.settings.model_output_characters:
                    raise ApplicationError("OUTPUT_TOO_LONG", "子任务结果超过长度限制", 422)
            if not output.strip():
                raise ApplicationError("EMPTY_MODEL_RESPONSE", "子 Agent 未返回正文", 502)
            public["output"] = output
            await self._finish(identifier, "completed", json.dumps(public, ensure_ascii=False), None)
            return {"ok": True, "audit_id": str(identifier), **public}
        except asyncio.CancelledError:
            await self._finish(identifier, "cancelled", json.dumps(public, ensure_ascii=False), "AGENT_CANCELLED")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ApplicationError) else "AGENT_FAILED"
            await self._finish(identifier, "failed", json.dumps(public, ensure_ascii=False), code)
            if code in {"ACCESS_REVOKED", "WORKSPACE_ACCESS_DENIED", "WORKSPACE_ARCHIVED"}:
                raise ApplicationError("ACCESS_REVOKED", "子任务权限已失效", 403) from exc
            return {"ok": False, "audit_id": str(identifier), **public, "error_code": code}
