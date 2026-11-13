"""本地问答 HTTP 与 SSE 接口。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.auth import ScopeDependency, auth_service
from app.core.errors import ApplicationError
from app.core.security import COOKIE_NAME
from app.models.conversation import Run
from app.schemas.approval import ApprovalDecisionInput, ApprovalView, RunTrace
from app.schemas.chat import (
    ConversationAgentInput,
    ConversationCreate,
    ConversationModelInput,
    ConversationView,
    MessageEdit,
    MessagePage,
    ModelOption,
    RegenerateInput,
    RunCreate,
    RunSnapshot,
)
from app.schemas.tools import ToolCallView
from app.services.agents import AgentService
from app.services.approvals import ApprovalService
from app.services.chat_runtime import ChatRuntime
from app.services.chat_store import ACTIVE
from app.services.model_catalog import is_reasoner, max_tokens_for, resolve_model
from app.services.tools import list_tool_calls

router = APIRouter(prefix="/api", tags=["问答"])
SSE_POLL_SECONDS = 0.25
HEARTBEAT_POLLS = 40


async def get_runtime(request: Request) -> ChatRuntime:
    """仅问答请求触发执行器初始化，健康检查不依赖业务表。

    Args:
        request: 当前请求。

    Returns:
        当前应用的生成管理器。
    """
    runtime: ChatRuntime = request.app.state.chat_runtime
    await runtime.ensure_ready()
    return runtime


RuntimeDependency = Annotated[ChatRuntime, Depends(get_runtime)]


def agent_service(request: Request) -> AgentService:
    """获取 Agent 配置服务。

    Args:
        request: 当前请求。

    Returns:
        Agent 服务。

    Raises:
        ApplicationError: 工作台未启用。
    """
    executor: AgentService | None = request.app.state.agent_service
    if executor is None:
        raise ApplicationError("AGENT_DISABLED", "Agent 工作台未启用", 503)
    return executor


def approval_service(request: Request) -> ApprovalService:
    """获取审批服务。

    Args:
        request: 当前请求。

    Returns:
        审批服务。

    Raises:
        ApplicationError: 审批未启用。
    """
    executor: ApprovalService | None = request.app.state.approval_service
    if executor is None:
        raise ApplicationError("APPROVAL_DISABLED", "写操作审批未启用", 503)
    return executor


@router.post("/conversations", response_model=ConversationView, status_code=201)
async def create_conversation(
    body: ConversationCreate, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> ConversationView:
    """创建空对话。

    Args:
        body: 标题。
        runtime: 应用执行器。

    Returns:
        新建对话。
    """
    if body.agent_profile_id is not None:
        await agent_service(request).ensure_usable_scope(scope, body.agent_profile_id)
    selected = body.model.strip() if body.model else None
    if selected == "":
        selected = None
    if selected is not None:
        resolve_model(runtime.settings, selected, None)
    return await runtime.store.create_conversation(body.title, scope, body.agent_profile_id, selected)


@router.get("/models", response_model=list[ModelOption])
async def models(_scope: ScopeDependency, runtime: RuntimeDependency) -> list[ModelOption]:
    """返回当前允许的问答模型。

    Args:
        _scope: 已登录用户。登录校验由依赖完成。
        runtime: 应用执行器。

    Returns:
        白名单中的模型及输出上限。
    """
    settings = runtime.settings
    return [
        ModelOption(
            id=name,
            max_tokens=max_tokens_for(settings, name),
            thinking=is_reasoner(settings, name),
            is_default=name == settings.deepseek_model,
        )
        for name in settings.allowed_models
    ]


@router.post("/conversations/{conversation_id}/model", response_model=ConversationView)
async def set_conversation_model(
    conversation_id: UUID,
    body: ConversationModelInput,
    request: Request,
    scope: ScopeDependency,
    runtime: RuntimeDependency,
) -> ConversationView:
    """设置或清除对话级模型。

    Args:
        conversation_id: 对话标识。
        body: 目标模型。
        request: 当前请求。
        scope: 已验证用户和空间。
        runtime: 应用执行器。

    Returns:
        更新后的对话。
    """
    await auth_service(request).conversation(scope, conversation_id)
    if body.model is not None:
        resolve_model(runtime.settings, body.model, None)
    return await runtime.store.set_conversation_model(conversation_id, body.model, scope)


@router.post("/conversations/{conversation_id}/agent", response_model=ConversationView)
async def switch_conversation_agent(
    conversation_id: UUID,
    body: ConversationAgentInput,
    request: Request,
    scope: ScopeDependency,
    runtime: RuntimeDependency,
) -> ConversationView:
    """切换对话使用的 Agent；生成中拒绝切换。

    Args:
        conversation_id: 对话标识。
        body: 目标 Agent；NULL 表示内置助手。
        request: 当前请求。
        scope: 已验证用户和空间。
        runtime: 应用执行器。

    Returns:
        更新后的对话。
    """
    await auth_service(request).conversation(scope, conversation_id)
    return await agent_service(request).switch_conversation_agent(scope, conversation_id, body.agent_profile_id)


@router.get("/conversations", response_model=list[ConversationView])
async def conversations(
    scope: ScopeDependency,
    runtime: RuntimeDependency,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ConversationView]:
    """读取对话列表。

    Args:
        runtime: 应用执行器。
        offset: 偏移量。
        limit: 最大数量。

    Returns:
        按最近更新时间递减的对话。
    """
    return await runtime.store.list_conversations(offset, limit, scope)


@router.get("/conversations/{conversation_id}/messages", response_model=MessagePage)
async def messages(
    conversation_id: UUID,
    request: Request,
    scope: ScopeDependency,
    runtime: RuntimeDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> MessagePage:
    """读取保存的消息并发现当前运行。

    Args:
        conversation_id: 对话标识。
        runtime: 应用执行器。
        after_sequence: 起始消息游标。
        limit: 页面大小。

    Returns:
        消息页。
    """
    await auth_service(request).conversation(scope, conversation_id)
    return await runtime.store.messages(conversation_id, after_sequence, limit)


@router.post("/conversations/{conversation_id}/runs", response_model=RunSnapshot, status_code=202)
async def create_run(
    conversation_id: UUID, body: RunCreate, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> RunSnapshot:
    """提交问题，事务成功后启动独立生成。

    Args:
        conversation_id: 对话标识。
        body: 问题与幂等键。
        request: HTTP 关联上下文。
        runtime: 应用执行器。

    Returns:
        可用于查询或订阅的运行快照。
    """
    await auth_service(request).conversation(scope, conversation_id)
    runtime.provider.validate()
    snapshot = await runtime.create_run(conversation_id, body, scope)
    if snapshot.run.status == "pending":
        runtime.schedule(snapshot.run.id, request.state.request_id)
    return snapshot


@router.post("/messages/{message_id}/regenerate", response_model=RunSnapshot, status_code=202)
async def regenerate_message(
    message_id: UUID, body: RegenerateInput, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> RunSnapshot:
    """重新生成最近一条助手回答，不追加用户消息。

    Args:
        message_id: 助手消息。
        body: 幂等键。
        request: HTTP 关联上下文。
        scope: 已验证用户和空间。
        runtime: 应用执行器。

    Returns:
        替代运行的快照。
    """
    runtime.provider.validate()
    snapshot = await runtime.replace_answer(message_id, body.idempotency_key, scope, content=None, role="assistant")
    if snapshot.run.status == "pending":
        runtime.schedule(snapshot.run.id, request.state.request_id)
    return snapshot


@router.post("/messages/{message_id}/edit", response_model=RunSnapshot, status_code=202)
async def edit_message(
    message_id: UUID, body: MessageEdit, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> RunSnapshot:
    """修改最近一条用户消息并重新生成其后的回答。

    Args:
        message_id: 用户消息。
        body: 新正文与幂等键。
        request: HTTP 关联上下文。
        scope: 已验证用户和空间。
        runtime: 应用执行器。

    Returns:
        替代运行的快照。
    """
    runtime.provider.validate()
    snapshot = await runtime.replace_answer(message_id, body.idempotency_key, scope, content=body.content, role="user")
    if snapshot.run.status == "pending":
        runtime.schedule(snapshot.run.id, request.state.request_id)
    return snapshot


@router.get("/runs/{run_id}", response_model=RunSnapshot)
async def run_snapshot(
    run_id: UUID, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> RunSnapshot:
    """查询运行的权威状态与持久化正文。

    Args:
        run_id: 运行标识。
        runtime: 应用执行器。

    Returns:
        运行快照。
    """
    await auth_service(request).run(scope, run_id)
    return await runtime.store.snapshot(run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunSnapshot)
async def cancel_run(run_id: UUID, request: Request, scope: ScopeDependency, runtime: RuntimeDependency) -> RunSnapshot:
    """停止生成，终态请求可安全重复。

    Args:
        run_id: 运行标识。
        runtime: 应用执行器。

    Returns:
        收敛后的快照。
    """
    await auth_service(request).run(scope, run_id)
    await runtime.store.cancel(run_id)
    await runtime.cancel(run_id)
    return await runtime.store.snapshot(run_id)


@router.get("/runs/{run_id}/events")
async def events(
    run_id: UUID, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> StreamingResponse:
    """流式发布完整快照，断线重连不会重复拼接文本。

    Args:
        run_id: 运行标识。
        request: 用于检查客户端断开。
        runtime: 应用执行器。

    Returns:
        无缓存的 SSE 响应。
    """
    await auth_service(request).run(scope, run_id)
    # 在发送 HTTP 响应头之前验证资源存在，避免把 404 藏在 200 的流中。
    await runtime.store.snapshot(run_id)

    async def stream() -> AsyncIterator[str]:
        """每轮查询后立即释放会话，不占用数据库事务等待。

        Yields:
            快照、终态或心跳帧。
        """
        previous, polls = "", 0
        while not await request.is_disconnected():
            try:
                identity = await auth_service(request).authenticate(request.cookies.get(COOKIE_NAME, ""))
                fresh = await auth_service(request).scope(identity, scope.workspace_id)
                await auth_service(request).run(fresh, run_id)
            except ApplicationError:
                yield 'event: access_revoked\ndata: {"code":"ACCESS_REVOKED"}\n\n'
                return
            snapshot = await runtime.store.snapshot(run_id)
            payload = snapshot.model_dump_json()
            terminal = snapshot.run.status not in ACTIVE
            if payload != previous or terminal:
                name = snapshot.run.status if terminal else "snapshot"
                yield f"event: {name}\ndata: {payload}\n\n"
                previous = payload
            if terminal:
                return
            polls += 1
            if polls % HEARTBEAT_POLLS == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/tool-calls", response_model=list[ToolCallView])
async def tool_calls(
    run_id: UUID, request: Request, scope: ScopeDependency, runtime: RuntimeDependency
) -> list[ToolCallView]:
    """只向运行所有者返回工具执行依据。

    Args:
        run_id: 当前运行。
        request: 可信认证上下文。
        scope: 已验证用户和空间。
        runtime: 应用执行器。

    Returns:
        工具调用安全记录。
    """
    await auth_service(request).run(scope, run_id)
    return await list_tool_calls(runtime.database.session_factory, run_id, scope)


@router.get("/runs/{run_id}/approvals", response_model=list[ApprovalView])
async def run_approvals(run_id: UUID, request: Request, scope: ScopeDependency) -> list[ApprovalView]:
    """读取当前运行的审批链。"""
    await auth_service(request).run(scope, run_id)
    return await approval_service(request).list_for_run(scope, run_id)


@router.post("/runs/{run_id}/approvals/{approval_id}/decide", response_model=ApprovalView)
async def decide_approval(
    run_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionInput,
    request: Request,
    scope: ScopeDependency,
) -> ApprovalView:
    """批准或拒绝一次写操作；只有运行所有者可以决策。"""
    await auth_service(request).run(scope, run_id)
    return await approval_service(request).decide(scope, run_id, approval_id, body.decision, body.comment)


@router.get("/runs/{run_id}/trace", response_model=RunTrace)
async def run_trace(run_id: UUID, request: Request, scope: ScopeDependency, runtime: RuntimeDependency) -> RunTrace:
    """读取一次运行的脱敏轨迹，仅运行所有者可访问。"""
    await auth_service(request).run(scope, run_id)
    async with runtime.database.session_factory() as session:
        run = await session.scalar(select(Run).where(Run.id == run_id))
        snapshot = dict(run.agent_snapshot) if run is not None and isinstance(run.agent_snapshot, dict) else {}
    calls = await list_tool_calls(runtime.database.session_factory, run_id, scope)
    approvals = await approval_service(request).list_for_run(scope, run_id)
    return RunTrace(
        run_id=run_id,
        agent_snapshot=snapshot,
        tool_calls=[item.model_dump(mode="json") for item in calls],
        approvals=approvals,
    )
