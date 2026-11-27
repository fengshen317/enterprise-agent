"""写操作审批的真实数据库验收。"""

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.capabilities.builtins.run_stats import run_stats_spec
from app.capabilities.builtins.scheduled import scheduled_specs
from app.capabilities.registry import ToolRegistry
from app.capabilities.spec import AgentAssembly
from app.models.agent import WorkspaceToolPolicy
from app.models.approval import Approval, ApprovalEvent
from app.models.tool_call import ToolCall
from app.schemas.chat import RunCreate
from app.services.agent_assembler import AgentAssembler
from app.services.approvals import ApprovalService
from app.services.chat_runtime import ChatRuntime
from app.services.tools import ToolExecutor
from tests.test_chat_integration import ControlledProvider, new_conversation
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration

TASK = {
    "name": "每周复盘",
    "prompt": "总结本周运行情况",
    "cron_expression": "0 9 * * 1",
    "timezone": "Asia/Shanghai",
}


async def write_executor(app: FastAPI, client: httpx.AsyncClient) -> ToolExecutor:
    """创建一个包含写工具的执行器，并领取真实运行。

    Args:
        app: 测试应用。
        client: 已登录客户端。

    Returns:
        绑定当前运行的执行器。
    """
    runtime = app.state.chat_runtime
    conversation = await new_conversation(client)
    snapshot = await runtime.create_run(
        UUID(conversation),
        RunCreate(content="创建定时任务", idempotency_key="approval-test"),
        app.state.test_scope,
    )
    await runtime.store.begin_run(snapshot.run.id)
    registry = ToolRegistry()
    registry.register(run_stats_spec(runtime.settings))
    for spec in scheduled_specs(runtime.settings):
        registry.register(spec)
    assembly = AgentAssembly(
        agent_id=None,
        name="写工具测试助手",
        version=1,
        instructions="",
        model=runtime.settings.deepseek_model,
        tools=registry.catalog(),
        skills=(),
        knowledge_scope={"mode": "all_readable"},
        allow_planning=True,
        allow_delegation=True,
        snapshot={},
    )
    return ToolExecutor(
        app.state.database.session_factory,
        runtime.settings,
        snapshot.run.id,
        assembly=assembly,
        registry=registry,
        approval_service=app.state.approval_service,
        schedule_store=runtime.schedule_store,
    )


async def wait_approval(client: httpx.AsyncClient, run_id: UUID) -> dict[str, Any]:
    """轮询到审批出现。

    Args:
        client: 已登录客户端。
        run_id: 当前运行。

    Returns:
        待审批记录。
    """
    for _ in range(100):
        rows = (await client.get(f"/api/runs/{run_id}/approvals")).json()
        if rows:
            return rows[0]
        await asyncio.sleep(0.02)
    raise AssertionError("审批未出现")


async def test_approve_executes_and_reject_has_no_side_effect(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """批准后才创建定时任务；拒绝不产生业务副作用。"""
    app, client, _ = chat
    executor = await write_executor(app, client)
    task = asyncio.create_task(
        executor.invoke("create_scheduled_task", {**TASK, "push_channels": ["dingtalk", "feishu", "dingtalk"]})
    )
    approval = await wait_approval(client, executor.run_id)
    assert approval["risk_level"] == "write"
    assert "创建定时任务" in approval["args_preview"]["summary"]
    assert approval["args_preview"]["push_channels"] == ["dingtalk", "feishu"]
    snapshot = (await client.get(f"/api/runs/{executor.run_id}")).json()
    assert snapshot["approval"]["id"] == approval["id"]
    decided = await client.post(
        f"/api/runs/{executor.run_id}/approvals/{approval['id']}/decide",
        json={"decision": "approve", "comment": "确认"},
    )
    assert decided.status_code == 200, decided.text
    result = await asyncio.wait_for(task, 5)
    assert '"ok": true' in result
    schedules = (await client.get("/api/schedules")).json()
    assert any(item["name"] == TASK["name"] and item["push_channels"] == ["dingtalk", "feishu"] for item in schedules)
    async with app.state.database.session_factory() as session:
        call = await session.scalar(select(ToolCall).where(ToolCall.run_id == executor.run_id))
        row = await session.scalar(select(Approval).where(Approval.run_id == executor.run_id))
        assert call is not None and call.status == "completed"
        assert row is not None and row.status == "executed"
    trace = await client.get(f"/api/runs/{executor.run_id}/trace")
    assert trace.status_code == 200
    assert trace.json()["agent_snapshot"]["agent_name"] == "序列助手"
    assert trace.json()["approvals"][0]["status"] == "executed"

    reject_executor = await write_executor(app, client)
    rejected = asyncio.create_task(reject_executor.invoke("create_scheduled_task", {**TASK, "name": "拒绝任务"}))
    reject_approval = await wait_approval(client, reject_executor.run_id)
    decision = await client.post(
        f"/api/runs/{reject_executor.run_id}/approvals/{reject_approval['id']}/decide",
        json={"decision": "reject", "comment": "不需要"},
    )
    assert decision.status_code == 200
    outcome = await asyncio.wait_for(rejected, 5)
    assert "APPROVAL_REJECTED" in outcome
    schedules = (await client.get("/api/schedules")).json()
    assert all(item["name"] != "拒绝任务" for item in schedules)


@pytest.mark.parametrize("archive_before_approval", [False, True])
async def test_private_agent_schedule_creates_conversation_atomically(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], archive_before_approval: bool
) -> None:
    """验证专用对话和任务的归属、审批前零副作用及审批后权限复核。

    Args:
        chat: 已登录的测试应用。
        archive_before_approval: 在等待审批时归档目标 Agent，验证提交失败不留下对话。
    """
    app, client, _ = chat
    agent = await client.post("/api/agents", json={"name": "专用统计助手"})
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]
    executor = await write_executor(app, client)
    before = (await client.get("/api/conversations")).json()
    arguments = {**TASK, "agent_profile_id": agent_id, "enabled": False, "push_to_wechat": True}
    task = asyncio.create_task(executor.invoke("create_scheduled_task", arguments))
    approval = await wait_approval(client, executor.run_id)
    preview = approval["args_preview"]
    assert agent_id in preview["effect"] and "专用统计助手" in preview["effect"]
    assert preview["enabled"] is False and preview["push_to_wechat"] is True
    assert (await client.get("/api/conversations")).json() == before
    if archive_before_approval:
        archived = await client.post(f"/api/agents/{agent_id}/archive")
        assert archived.status_code == 200, archived.text
    decided = await client.post(
        f"/api/runs/{executor.run_id}/approvals/{approval['id']}/decide", json={"decision": "approve"}
    )
    assert decided.status_code == 200, decided.text
    result = json.loads(await asyncio.wait_for(task, 5))
    after = (await client.get("/api/conversations")).json()
    if archive_before_approval:
        assert result["error_code"] == "AGENT_UNAVAILABLE"
        assert after == before
        assert (await client.get("/api/schedules")).json() == []
        return
    assert result["ok"] is True
    item = result["data"]
    assert item["agent_profile_id"] == agent_id
    assert item["url"] == f"/schedules?schedule={item['id']}"
    dedicated = next(row for row in after if row["id"] == item["conversation_id"])
    assert dedicated["agent_profile_id"] == agent_id and dedicated["title"] == TASK["name"]
    assert [row for row in after if row != dedicated] == before
    detail = await client.get(f"/api/schedules/{item['id']}")
    assert detail.status_code == 200 and detail.json()["conversation_id"] == dedicated["id"]
    assert detail.json()["enabled"] is False and detail.json()["push_to_wechat"] is True


async def test_approved_schedule_control_changes_real_state(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """内置助手可先查询任务，再经逐次审批真实停用，而不是只回复成功。"""
    app, client, _ = chat
    conversation_id = await new_conversation(client)
    created = await client.post(
        "/api/schedules",
        json={**TASK, "conversation_id": conversation_id, "enabled": True, "push_to_wechat": False},
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    executor = await write_executor(app, client)
    default_tools = AgentAssembler(
        executor.factory,
        executor.registry,
        executor.settings,
    ).default_assembly().tool_keys
    assert "list_scheduled_tasks" in default_tools
    assert "set_scheduled_task_enabled" in default_tools
    assert "create_scheduled_task" in default_tools
    listed = await executor.invoke("list_scheduled_tasks", {})
    assert schedule_id in listed
    task = asyncio.create_task(
        executor.invoke("set_scheduled_task_enabled", {"schedule_id": schedule_id, "enabled": False})
    )
    await asyncio.sleep(0.05)
    assert not task.done(), await task
    approval = await wait_approval(client, executor.run_id)
    assert approval["args_preview"]["summary"] == "停用定时任务"
    decided = await client.post(
        f"/api/runs/{executor.run_id}/approvals/{approval['id']}/decide",
        json={"decision": "approve"},
    )
    assert decided.status_code == 200, decided.text
    assert '"enabled": false' in await asyncio.wait_for(task, 5)
    rows = (await client.get("/api/schedules")).json()
    assert next(item for item in rows if item["id"] == schedule_id)["enabled"] is False


async def test_approval_owner_scope_and_timeout(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """非运行所有者不能决策；超时自动过期。"""
    app, client, _ = chat
    executor = await write_executor(app, client)
    task = asyncio.create_task(executor.invoke("create_scheduled_task", {**TASK, "name": "超时任务"}))
    approval = await wait_approval(client, executor.run_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other") as other:
        from tests.auth_helpers import add_identity

        await add_identity(app, other, workspace=None, role="admin")
        denied = await other.post(
            f"/api/runs/{executor.run_id}/approvals/{approval['id']}/decide",
            json={"decision": "approve"},
        )
        assert denied.status_code == 404
    await app.state.approval_service.decide(app.state.test_scope, executor.run_id, UUID(approval["id"]), "reject", None)
    assert "APPROVAL_REJECTED" in await asyncio.wait_for(task, 5)

    fast = await write_executor(app, client)
    fast_settings = fast.settings.model_copy(update={"approval_timeout_seconds": 0.05})
    fast.settings = fast_settings
    fast.approval_service = ApprovalService(app.state.database.session_factory, fast_settings)
    timeout_task = asyncio.create_task(fast.invoke("create_scheduled_task", {**TASK, "name": "超时任务2"}))
    outcome = await asyncio.wait_for(timeout_task, 5)
    assert "APPROVAL_EXPIRED" in outcome


async def test_restart_expires_pending_approval(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """服务重启把等待审批收敛为过期，不恢复半完成写操作。"""
    app, client, _ = chat
    executor = await write_executor(app, client)
    task = asyncio.create_task(executor.invoke("create_scheduled_task", {**TASK, "name": "重启任务"}))
    approval = await wait_approval(client, executor.run_id)
    await app.state.chat_runtime.store.recover()
    outcome = await asyncio.wait_for(task, 5)
    assert "APPROVAL_EXPIRED" in outcome
    snapshot = (await client.get(f"/api/runs/{executor.run_id}")).json()
    assert snapshot["run"]["status"] == "failed"
    assert snapshot["run"]["error_code"] == "PROCESS_RESTARTED"
    approvals = (await client.get(f"/api/runs/{executor.run_id}/approvals")).json()
    assert approvals[0]["id"] == approval["id"] and approvals[0]["status"] == "expired"


async def approve_schedule(app: FastAPI, client: httpx.AsyncClient) -> tuple[ToolExecutor, UUID, UUID]:
    """通过实际审批接口准备已批准、尚未执行业务写入的测试断点。

    Args:
        app: 使用独立测试库的应用。
        client: 当前成员的登录会话。

    Returns:
        执行器、工具调用标识和审批标识。
    """
    executor = await write_executor(app, client)
    service = app.state.approval_service
    spec = executor.assembly.tool_map["create_scheduled_task"]
    call_id, _ = await executor._begin(spec.key, 1, spec)
    args = spec.args_model.model_validate({**TASK, "prompt": "长问题" * 120})
    waiting = asyncio.create_task(service.request_and_wait(executor, spec, args, call_id))
    approval = await wait_approval(client, executor.run_id)
    decision = await client.post(
        f"/api/runs/{executor.run_id}/approvals/{approval['id']}/decide", json={"decision": "approve"}
    )
    assert decision.is_success and await waiting == "approved"
    app.state.chat_runtime.settings.schedule_enabled = True
    return executor, call_id, UUID(approval["id"])


@pytest.mark.parametrize("checkpoint", ["approved", "executed", "policy_revoked", "changed_arguments"])
async def test_restart_recovers_approved_schedule_once(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], checkpoint: str
) -> None:
    """覆盖批准后和业务提交后两个断点，并验证并发恢复使用同一个结果。"""
    app, client, _ = chat
    executor, call_id, approval_id = await approve_schedule(app, client)
    runtime = app.state.chat_runtime
    service = app.state.approval_service
    spec = executor.assembly.tool_map["create_scheduled_task"]
    if checkpoint == "policy_revoked":
        scope = app.state.test_scope
        async with executor.factory.begin() as session:
            session.add(
                WorkspaceToolPolicy(
                    workspace_id=scope.workspace_id, tool_key=spec.key, enabled=False, updated_by_user_id=scope.user_id
                )
            )
    if checkpoint == "changed_arguments":
        async with executor.factory.begin() as session:
            row = await session.get(Approval, approval_id)
            assert row is not None and row.execution_args is not None
            row.execution_args = {**row.execution_args, "name": "未批准的改名"}
    if checkpoint == "executed":
        results = await asyncio.gather(
            *(runtime.schedule_store.execute_approved(executor.run_id, call_id) for _ in range(2))
        )
        assert results[0].id == results[1].id
    await runtime.close()
    restarted = ChatRuntime(
        runtime.database,
        runtime.settings,
        runtime.provider,
        schedule_store=runtime.schedule_store,
        approval_service=service,
    )
    try:
        await asyncio.gather(restarted.ensure_ready(), restarted.ensure_ready())
    finally:
        await restarted.close()
    schedules = (await client.get("/api/schedules")).json()
    snapshot = await runtime.store.snapshot(executor.run_id)
    errors = {"policy_revoked": "TOOL_NOT_ALLOWED", "changed_arguments": "APPROVAL_CHANGED"}
    if checkpoint in errors:
        assert schedules == [] and snapshot.run.error_code == errors[checkpoint]
        return
    assert len(schedules) == 1 and schedules[0]["prompt"] == "长问题" * 120
    async with executor.factory() as session:
        actions = await session.scalars(select(ApprovalEvent.action).where(ApprovalEvent.approval_id == approval_id))
        assert sorted(actions) == ["approved", "executed", "requested"]
        call = await session.get(ToolCall, call_id)
        assert call is not None and call.status == "completed"
        assert call.result and json.loads(call.result)["schedule_id"] == schedules[0]["id"]
    assert snapshot.run.status == "completed"
    assert f"/schedules?schedule={schedules[0]['id']}" in snapshot.message.content


@pytest.mark.parametrize("stage", ["approved", "created", "deleted"])
@pytest.mark.parametrize("restart", [False, True])
async def test_stop_during_schedule_creation(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], stage: str, restart: bool
) -> None:
    """覆盖取消先于或晚于业务提交，以及取消落库后服务退出的组合。"""
    app, client, _ = chat
    executor, call_id, approval_id = await approve_schedule(app, client)
    runtime = app.state.chat_runtime
    committed = stage != "approved"
    if committed:
        await runtime.schedule_store.execute_approved(executor.run_id, call_id)
    if stage == "deleted":
        response = await client.post(f"/api/schedules/{call_id}/delete")
        assert response.status_code == 204
    partial = "已读取配置。"
    if stage == "created" and not restart:
        partial += f"[已有任务链接](/schedules?schedule={call_id})"
    await runtime.store.save_content(executor.run_id, partial)
    if restart:
        await runtime.store.cancel(executor.run_id)
        # 旧执行器可能在停止已落库后才收到异常，其回调不能覆盖业务事实。
        await app.state.approval_service.mark_failed(call_id)
        await executor._finish(call_id, "failed", None, "ACCESS_REVOKED")
        await runtime.close()
        restarted = ChatRuntime(
            runtime.database,
            runtime.settings,
            runtime.provider,
            schedule_store=runtime.schedule_store,
            approval_service=app.state.approval_service,
        )
        try:
            await restarted.ensure_ready()
        finally:
            await restarted.close()
    else:
        stop = await client.post(f"/api/runs/{executor.run_id}/cancel")
        assert stop.is_success
    snapshot = await runtime.store.snapshot(executor.run_id)
    assert snapshot.run.status == "cancelled" and snapshot.run.error_code is None
    assert snapshot.message.content.startswith(partial)
    assert snapshot.message.content.count(f"/schedules?schedule={call_id}") == int(stage == "created")
    assert ("该任务现已删除" in snapshot.message.content) == (stage == "deleted")
    assert len((await client.get("/api/schedules")).json()) == int(stage == "created")
    async with executor.factory() as session:
        row = await session.get(Approval, approval_id)
        call = await session.get(ToolCall, call_id)
        assert row and row.status == ("executed" if committed else "cancelled")
        actions = await session.scalars(select(ApprovalEvent.action).where(ApprovalEvent.approval_id == approval_id))
        assert sorted(actions) == sorted(["requested", "approved", row.status])
        assert call and call.status == ("completed" if committed else "cancelled")
        if committed:
            assert call.result and json.loads(call.result)["schedule_id"] == str(call_id)
            assert bool(json.loads(call.result).get("schedule_deleted")) == (stage == "deleted")
            assert call.error_code is None
