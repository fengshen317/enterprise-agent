"""Agent 配置、技能版本与工具目录的真实数据库验收。"""

from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models.conversation import Run
from app.models.identity import Workspace
from app.schemas.chat import RunCreate
from app.services.chat_agent import ChatAgentExecutor
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, settled, submit
from tests.test_chat_integration import chat as chat
from tests.test_tools_integration import executor_for

pytestmark = pytest.mark.integration


async def workspace_slug(app: FastAPI, workspace_id: object) -> str:
    """读取测试空间的稳定 slug，供同空间第二个身份加入。

    Args:
        app: 测试应用。
        workspace_id: 空间标识。

    Returns:
        空间 slug。
    """
    async with app.state.database.session_factory() as session:
        slug = await session.scalar(select(Workspace.slug).where(Workspace.id == workspace_id))
    assert slug is not None
    return slug


def agent_body(**overrides: object) -> dict[str, object]:
    """构造最小 Agent 输入。

    Args:
        overrides: 覆盖字段。

    Returns:
        请求体。
    """
    body: dict[str, object] = {
        "name": "统计助手",
        "description": "只读统计",
        "instructions": "只做只读统计，回答必须附来源。",
        "visibility": "private",
        "tool_keys": ["get_my_run_stats"],
        "skills": [],
        "knowledge_mode": "all_readable",
        "starter_prompts": ["查询最近七天运行统计"],
    }
    body.update(overrides)
    return body


async def test_private_agent_permissions_and_version_conflict(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """私有 Agent 仅 owner 可读，管理员不能读取；空间 Agent 由管理员管理。"""
    app, client, _ = chat
    scope = app.state.test_scope
    slug = await workspace_slug(app, scope.workspace_id)
    created = await client.post("/api/agents", json=agent_body())
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["can_manage"] is True and agent["is_owner"] is True
    assert agent["instructions"] is not None
    assert agent["tools"][0]["key"] == "get_my_run_stats"
    agent_id = agent["id"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin") as admin:
        await add_identity(app, admin, workspace=slug, role="admin")
        assert (await admin.get(f"/api/agents/{agent_id}")).status_code == 404
        hidden = await admin.post("/api/agents", json=agent_body(tool_keys=[]))
        assert hidden.status_code == 201
        space_agent = await admin.post(
            "/api/agents", json=agent_body(name="空间助手", visibility="workspace", tool_keys=[])
        )
        assert space_agent.status_code == 201, space_agent.text
        space_id = space_agent.json()["id"]
        assert (await admin.get(f"/api/agents/{space_id}")).status_code == 200
        member_update = await admin.post(
            f"/api/agents/{space_id}",
            json={**agent_body(name="成员改"), "expected_version": 1, "visibility": "workspace"},
        )
        assert member_update.status_code == 200, member_update.text
        stale = await admin.post(
            f"/api/agents/{space_id}",
            json={**agent_body(name="过期"), "expected_version": 1, "visibility": "workspace"},
        )
        assert stale.status_code == 409
        archived = await admin.post(f"/api/agents/{space_id}/archive")
        assert archived.status_code == 200 and archived.json()["status"] == "archived"
        still_visible = await admin.post(
            "/api/agents", json=agent_body(name="仍在用", visibility="workspace", tool_keys=[])
        )
        assert still_visible.status_code == 201

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://member") as member:
        await add_identity(app, member, workspace=slug, role="member")
        forbidden = await member.post("/api/agents", json=agent_body(visibility="workspace"))
        assert forbidden.status_code == 403
        listing = await member.get("/api/agents")
        assert listing.status_code == 200
        assert any(item["can_manage"] is False for item in listing.json())


async def test_skill_versions_pin_agent_binding(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """技能发布后才能绑定；新版本不会悄悄改变已绑定 Agent。"""
    app, client, _ = chat
    skill_body = {
        "key": "policy_qa",
        "name": "制度问答",
        "description": "制度查询流程",
        "visibility": "private",
        "content_md": "# 步骤\n1. 检索知识库\n2. 引用来源",
        "tool_keys": ["get_my_run_stats"],
        "activation": "always",
    }
    created = await client.post("/api/skills", json=skill_body)
    assert created.status_code == 201, created.text
    skill = created.json()
    assert skill["status"] == "draft" and skill["current_version"] == 1
    skill_id = skill["id"]

    draft_bind = await client.post("/api/agents", json=agent_body(skills=[{"id": skill_id, "activation": "always"}]))
    assert draft_bind.status_code == 422
    published = await client.post(f"/api/skills/{skill_id}/publish", json={"version": 1})
    assert published.status_code == 200 and published.json()["status"] == "published"

    bound = await client.post("/api/agents", json=agent_body(skills=[{"id": skill_id, "activation": "always"}]))
    assert bound.status_code == 201, bound.text
    agent_id = bound.json()["id"]
    assert bound.json()["skills"][0]["version"] == 1

    updated = await client.post(
        f"/api/skills/{skill_id}",
        json={**skill_body, "content_md": "# 步骤\n1. 检索并复核\n2. 引用来源", "change_note": "补充复核"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "draft" and updated.json()["current_version"] == 2
    still_bound = await client.get(f"/api/agents/{agent_id}")
    assert still_bound.json()["skills"][0]["version"] == 1
    # 技能有新草稿时，已绑定旧版本的 Agent 仍可保存，不会被强制升级。
    unchanged = await client.post(
        f"/api/agents/{agent_id}",
        json={**agent_body(skills=[{"id": skill_id, "activation": "always"}]), "expected_version": 1},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["skills"][0]["version"] == 1

    assert (await client.post(f"/api/skills/{skill_id}/publish", json={"version": 2})).status_code == 200
    rebound = await client.post(
        f"/api/agents/{agent_id}",
        json={
            **agent_body(skills=[{"id": skill_id, "activation": "on_demand"}]),
            "expected_version": 2,
        },
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["skills"][0]["version"] == 2
    assert rebound.json()["skills"][0]["activation"] == "on_demand"
    versions = await client.get(f"/api/skills/{skill_id}/versions")
    assert [item["version"] for item in versions.json()] == [2, 1]


async def test_conversation_binding_and_run_snapshot(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """对话绑定 Agent，提交时固定快照，生成中拒绝切换。"""
    app, client, _ = chat
    first = await client.post("/api/agents", json=agent_body(name="第一助手"))
    second = await client.post("/api/agents", json=agent_body(name="第二助手", tool_keys=[]))
    assert first.status_code == 201 and second.status_code == 201
    first_id = first.json()["id"]
    second_id = second.json()["id"]
    created = await client.post("/api/conversations", json={"title": "绑定测试", "agent_profile_id": first_id})
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["agent_profile_id"] == first_id
    conversation_id = conversation["id"]

    snapshot = await submit(client, conversation_id, "查询统计")
    terminal = await settled(client, snapshot["run"]["id"])
    assert terminal["run"]["status"] == "completed", terminal
    async with app.state.database.session_factory() as session:
        run = await session.scalar(select(Run).where(Run.id == snapshot["run"]["id"]))
        assert run is not None
        stored = run.agent_snapshot
        assert stored["agent_name"] == "第一助手"
        assert stored["instructions"]
        assert {"key": "get_my_run_stats", "version": 1} in stored["tools"]

    switched = await client.post(f"/api/conversations/{conversation_id}/agent", json={"agent_profile_id": second_id})
    assert switched.status_code == 200, switched.text
    assert switched.json()["agent_profile_id"] == second_id

    pending = await app.state.chat_runtime.store.create_run(
        conversation_id,
        RunCreate(content="保持运行", idempotency_key="active-run"),
        "test",
        app.state.test_scope,
    )
    busy = await client.post(f"/api/conversations/{conversation_id}/agent", json={"agent_profile_id": first_id})
    assert busy.status_code == 409
    await app.state.chat_runtime.store.cancel(pending.run.id)
    await app.state.chat_runtime.store.finish(pending.run.id, "", "cancelled")
    reset = await client.post(f"/api/conversations/{conversation_id}/agent", json={"agent_profile_id": None})
    assert reset.status_code == 200 and reset.json()["agent_profile_id"] is None


async def test_assembler_injects_always_and_exposes_on_demand(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """常驻技能进入提示词；按需技能只暴露 load_skill。"""
    app, client, _ = chat
    always = await client.post(
        "/api/skills",
        json={
            "key": "always_skill",
            "name": "常驻技能",
            "visibility": "private",
            "content_md": "ALWAYS-SKILL",
            "tool_keys": ["get_my_run_stats"],
            "activation": "always",
        },
    )
    demand = await client.post(
        "/api/skills",
        json={
            "key": "demand_skill",
            "name": "按需技能",
            "visibility": "private",
            "content_md": "DEMAND-SKILL",
            "tool_keys": ["get_my_run_stats"],
            "activation": "on_demand",
        },
    )
    assert always.status_code == 201 and demand.status_code == 201
    for item in (always.json(), demand.json()):
        assert (await client.post(f"/api/skills/{item['id']}/publish", json={"version": 1})).status_code == 200
    created = await client.post(
        "/api/agents",
        json=agent_body(
            skills=[
                {"id": always.json()["id"], "activation": "always"},
                {"id": demand.json()["id"], "activation": "on_demand"},
            ]
        ),
    )
    assert created.status_code == 201, created.text
    runtime = app.state.chat_runtime
    assembly = await runtime.assembler.assemble(app.state.test_scope, UUID(created.json()["id"]))
    prompt = assembly.system_prompt()
    assert "ALWAYS-SKILL" in prompt
    assert "DEMAND-SKILL" not in prompt
    assert "demand_skill" in prompt
    base = await executor_for(app, client)
    executor = ChatAgentExecutor(base.factory, base.settings, base.run_id, assembly=assembly)
    assert "load_skill" in executor.assembly.tool_keys


async def test_tool_catalog_policy_and_skill_subset(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """工具目录、空间启停策略和技能工具子集校验。"""
    app, client, _ = chat
    scope = app.state.test_scope
    slug = await workspace_slug(app, scope.workspace_id)
    tools = await client.get("/api/tools")
    assert tools.status_code == 200
    catalog = {item["key"]: item for item in tools.json()}
    assert catalog["get_my_run_stats"]["approval"] == "never"
    assert catalog["get_my_run_stats"]["risk"] == "read"
    assert "properties" in catalog["get_my_run_stats"]["input_schema"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://member") as member:
        await add_identity(app, member, workspace=slug, role="member")
        denied = await member.post("/api/admin/tools/get_my_run_stats", json={"enabled": False})
        assert denied.status_code == 403
    disabled = await client.post("/api/admin/tools/get_my_run_stats", json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    blocked = await client.post("/api/agents", json=agent_body())
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "TOOL_UNAVAILABLE"
    assert (await client.post("/api/admin/tools/get_my_run_stats", json={"enabled": True})).status_code == 200

    skill = await client.post(
        "/api/skills",
        json={
            "key": "stats_skill",
            "name": "统计技能",
            "visibility": "private",
            "content_md": "先查询统计再总结。",
            "tool_keys": ["get_my_run_stats"],
            "activation": "always",
        },
    )
    assert skill.status_code == 201
    skill_id = skill.json()["id"]
    assert (await client.post(f"/api/skills/{skill_id}/publish", json={"version": 1})).status_code == 200
    missing_tool = await client.post(
        "/api/agents",
        json=agent_body(tool_keys=[], skills=[{"id": skill_id, "activation": "always"}]),
    )
    assert missing_tool.status_code == 422 and missing_tool.json()["error"]["code"] == "SKILL_TOOL_NOT_BOUND"
    valid = await client.post(
        "/api/agents",
        json=agent_body(skills=[{"id": skill_id, "activation": "always"}]),
    )
    assert valid.status_code == 201, valid.text
