"""用真实问答图和数据库验证聊天创建及定时运行之间的连接。"""

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.agents.provider import DeepSeekProvider
from app.capabilities.builtins.scheduled import scheduled_specs
from app.schemas.chat import RunCreate
from app.services.tools import ToolExecutor
from tests.test_approvals_integration import wait_approval
from tests.test_chat_agent_integration import response
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat
from tests.test_schedules_integration import schedule_body

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("decision", ["reject", "revoke"])
async def test_draft_creation_requires_current_approval_and_policy(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], decision: str
) -> None:
    """审批拒绝或等待期间停用写工具时，用户的技能库保持空白。"""
    app, client, _ = chat
    runtime = app.state.chat_runtime
    scope = app.state.test_scope
    snapshot = await runtime.create_run(
        UUID(await new_conversation(client)), RunCreate(content="保存草稿", idempotency_key=decision), scope
    )
    run_id = snapshot.run.id
    await runtime.store.begin_run(run_id)
    assembly = await runtime.assembler.assemble_for_run(run_id)
    executor = ToolExecutor(
        app.state.database.session_factory,
        runtime.settings,
        run_id,
        assembly=assembly,
        registry=runtime.registry,
        approval_service=app.state.approval_service,
    )
    action = "create_private_skill"
    draft = {"key": "brief", "name": "日报", "content_md": "记录当日重点"}
    task = asyncio.create_task(executor.invoke(action, draft))
    pending = await wait_approval(client, run_id)
    assert pending["args_preview"]["content_md"] == draft["content_md"]
    assert (await client.get("/api/skills")).json() == []
    if decision == "revoke":
        assert (await client.post(f"/api/admin/tools/{action}", json={"enabled": False})).is_success
        assert action not in (await runtime.assembler.assemble(scope, None)).tool_keys
    result = await client.post(
        f"/api/runs/{run_id}/approvals/{pending['id']}/decide",
        json={
            "decision": "reject" if decision == "reject" else "approve",
        },
    )
    assert result.is_success
    outcome = json.loads(await asyncio.wait_for(task, 5))
    assert outcome["error_code"] == ("APPROVAL_REJECTED" if decision == "reject" else "TOOL_NOT_ALLOWED")
    assert (await client.get("/api/skills")).json() == []


@pytest.mark.parametrize("decision", ["approve", "reject", "conflict"])
async def test_agent_edits_existing_schedule_with_reviewed_version(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], decision: str
) -> None:
    """只有审批通过且配置版本一致时才原地修改指定字段。"""
    app, client, _ = chat
    runtime = app.state.chat_runtime
    for spec in scheduled_specs(runtime.settings):
        if runtime.registry.resolve(spec.key) is None:
            runtime.registry.register(spec)
    conversation = await new_conversation(client)
    created = await client.post("/api/schedules", json=schedule_body(conversation, prompt="原始指令"))
    assert created.status_code == 201, created.text
    item = created.json()
    resource = f"/api/schedules/{item['id']}"

    snapshot = await runtime.create_run(
        UUID(conversation), RunCreate(content="把任务改到下午", idempotency_key=decision), app.state.test_scope
    )
    run_id = snapshot.run.id
    await runtime.store.begin_run(run_id)
    assembly = await runtime.assembler.assemble_for_run(run_id)
    assert {"get_scheduled_task", "update_scheduled_task"} <= assembly.tool_keys
    executor = ToolExecutor(
        app.state.database.session_factory,
        runtime.settings,
        run_id,
        assembly=assembly,
        registry=runtime.registry,
        approval_service=app.state.approval_service,
        schedule_store=app.state.schedule_runtime.store,
    )
    detail = json.loads(await executor.invoke("get_scheduled_task", {"schedule_id": item["id"]}))
    assert detail["data"]["prompt"] == "原始指令"
    changes = {
        "schedule_id": item["id"], "revision": detail["data"]["revision"],
        "cron_expression": "30 14 * * *", "calendar_mode": "cn_a_share",
    }
    update_task = asyncio.create_task(executor.invoke("update_scheduled_task", changes))
    pending = await wait_approval(client, run_id)
    assert pending["args_preview"]["cron_expression"] == {"before": "0 9 * * *", "after": "30 14 * * *"}
    assert pending["args_preview"]["calendar_mode"]["after"] == "沪深 A 股交易日"
    assert (await client.get(resource)).json() == item
    if decision == "conflict":
        assert (await client.post(f"{resource}/wechat-push", json={"push_to_wechat": True})).is_success
    result = await client.post(
        f"/api/runs/{run_id}/approvals/{pending['id']}/decide",
        json={"decision": "reject" if decision == "reject" else "approve"},
    )
    assert result.is_success
    outcome = json.loads(await asyncio.wait_for(update_task, 5))
    changed = (await client.get(resource)).json()
    assert changed["id"] == item["id"]
    assert changed["conversation_id"] == conversation
    assert changed["prompt"] == "原始指令"
    if decision == "approve":
        assert outcome["ok"] is True
        assert changed["cron_expression"] == "30 14 * * *"
        assert changed["calendar_mode"] == "cn_a_share"
    else:
        assert outcome["error_code"] == ("APPROVAL_REJECTED" if decision == "reject" else "SCHEDULE_VERSION_CONFLICT")
        assert changed["cron_expression"] == "0 9 * * *"
        assert changed["push_to_wechat"] is (decision == "conflict")


async def test_chat_creates_and_schedules_a_versioned_assistant(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """逐步批准四次写入，检查新任务运行时确实加载审批过的技能版本。"""
    from langchain_deepseek import ChatDeepSeek

    app, client, _ = chat
    runtime = app.state.chat_runtime
    runtime.settings = runtime.settings.model_copy(update={"deepseek_api_key": SecretStr("test-only")})
    runtime.provider = DeepSeekProvider(runtime.settings)
    for spec in scheduled_specs(runtime.settings):
        runtime.registry.register(spec)
    content = "仅按真实运行统计生成日报。"
    skill_body = {"key": "daily_stats", "name": "运行日报", "content_md": content, "tool_keys": ["get_my_run_stats"]}
    scheduled = False
    scheduled_requests: list[dict[str, Any]] = []

    def model_response(request: httpx.Request) -> httpx.Response:
        """使用上一个工具实际返回的标识构造下一步供应商响应。"""
        body = json.loads(request.content)
        if scheduled:
            scheduled_requests.append(body)
        tools = {tool["function"]["name"] for tool in body.get("tools", [])}
        results = [json.loads(row["content"]) for row in body["messages"] if row["role"] == "tool"]
        assert all(item["ok"] for item in results), results
        if scheduled:
            if not results:
                return response(name="get_my_run_stats", arguments={"days": 1})
            return response(content="运行日报已生成。")
        assert {
            "create_private_skill",
            "publish_private_skill",
            "create_private_agent",
            "create_scheduled_task",
        } <= tools
        if not results:
            return response(name="create_private_skill", arguments=skill_body)
        skill = results[0]["data"]
        reference = {"skill_id": skill["id"], "version": skill["version"]}
        if len(results) == 1:
            return response(name="publish_private_skill", arguments=reference)
        if len(results) == 2:
            return response(
                name="create_private_agent",
                arguments={
                    "name": "我的日报助手",
                    "instructions": "执行日报技能",
                    "skills": [reference],
                    "tool_keys": ["get_my_run_stats"],
                },
            )
        if len(results) == 3:
            return response(
                name="create_scheduled_task",
                arguments={
                    "name": "每天九点",
                    "prompt": "生成运行日报",
                    "cron_expression": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                    "agent_profile_id": results[2]["data"]["id"],
                },
            )
        return response(content="日报助手和定时任务已创建。")

    with monkeypatch.context() as mocks:

        def model(**kwargs: Any) -> ChatDeepSeek:
            """保留真实 SDK、模型协议和工具授权，只替换上游 HTTP 传输。"""
            return ChatDeepSeek(
                **kwargs, http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(model_response))
            )

        mocks.setattr("langchain_deepseek.ChatDeepSeek", model)
        conversation = await new_conversation(client)
        run_id = (await submit(client, conversation, "把运行日报做成我的助手，每天九点运行。"))["run"]["id"]
        async with asyncio.timeout(60):
            for key in (
                "create_private_skill",
                "publish_private_skill",
                "create_private_agent",
                "create_scheduled_task",
            ):
                while True:
                    snap = (await client.get(f"/api/runs/{run_id}")).json()
                    assert snap["run"]["status"] in {"pending", "running"}, snap
                    approval = snap.get("approval")
                    if approval and approval["tool_name"] == key:
                        decided = await client.post(
                            f"/api/runs/{run_id}/approvals/{approval['id']}/decide", json={"decision": "approve"}
                        )
                        assert decided.is_success, decided.text
                        break
                    await asyncio.sleep(0.04)
        assert (await settled(client, run_id))["run"]["status"] == "completed"
        (task,) = (await client.get("/api/schedules")).json()
        assert task["conversation_id"] != conversation and task["enabled"] and not task["push_to_wechat"]
        (skill,) = (await client.get("/api/skills")).json()
        (agent,) = (await client.get("/api/agents")).json()
        assert skill["visibility"] == agent["visibility"] == "private"
        assert skill["status"] == "published"
        assert (await client.post(f"/api/skills/{skill['id']}", json={**skill_body, "content_md": "第二版"})).is_success
        assert (await client.post(f"/api/skills/{skill['id']}/publish", json={"version": 2})).is_success
        scheduled = True
        triggered = await client.post(f"/api/schedules/{task['id']}/run")
        assert triggered.status_code == 202, triggered.text
        next_run = triggered.json()["run"]["id"]
        final = await settled(client, next_run)
        assert final["run"]["status"] == "completed", (
            final["run"]["error_code"],
            [[r.get("name") or r["role"] for r in call["messages"]] for call in scheduled_requests],
            [row["error_code"] for row in (await client.get(f"/api/runs/{next_run}/tool-calls")).json()],
        )
        assert final["message"]["content"] == "运行日报已生成。"
        assert content in str(scheduled_requests[0]["messages"][0]["content"])
        assert "create_private_agent" not in {tool["function"]["name"] for tool in scheduled_requests[0]["tools"]}
        snapshot = (await client.get(f"/api/runs/{next_run}/trace")).json()["agent_snapshot"]
        assert snapshot["agent_profile_id"] == agent["id"]
        assert [(item["id"], item["version"]) for item in snapshot["skills"]] == [(skill["id"], 1)]
