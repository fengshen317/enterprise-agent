"""写操作审批的真实数据库验收。"""

import asyncio
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
from app.models.approval import Approval
from app.models.tool_call import ToolCall
from app.schemas.chat import RunCreate
from app.services.agent_assembler import AgentAssembler
from app.services.approvals import ApprovalService
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
    task = asyncio.create_task(executor.invoke("create_scheduled_task", dict(TASK)))
    approval = await wait_approval(client, executor.run_id)
    assert approval["risk_level"] == "write"
    assert "创建定时任务" in approval["args_preview"]["summary"]
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
    assert any(item["name"] == TASK["name"] for item in schedules)
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
    assert "create_scheduled_task" not in default_tools
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
