"""真实 DeepSeek 对话规划及子 Agent 验收，仅在独立测试库创建临时资料。"""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import Settings
from app.core.database import Database
from app.main import create_app
from app.models.conversation import Conversation, Message, Run
from app.models.identity import AuthSession, User, Workspace, WorkspaceMembership
from app.services.accounts import AccountManager


async def cleanup(database: Database, user_id: UUID, workspace_id: UUID) -> None:
    """按本次已知身份清理关联资料，遵守消息到运行再到对话的外键顺序。

    Args:
        database: 专用测试库。
        user_id: 当前验收创建的用户。
        workspace_id: 当前验收创建的空间。
    """
    async with database.session_factory.begin() as session:
        conversations = select(Conversation.id).where(
            Conversation.created_by_user_id == user_id, Conversation.workspace_id == workspace_id
        )
        await session.execute(delete(Message).where(Message.conversation_id.in_(conversations)))
        await session.execute(delete(Run).where(Run.conversation_id.in_(conversations)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversations)))
        await session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
        await session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.user_id == user_id))
        await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
        await session.execute(delete(User).where(User.id == user_id))


async def main() -> bool:
    """验证登录、对话规划、真实工具、子 Agent 和汇总。

    Returns:
        实际模型与数据库闭环是否通过。
    """
    source = Settings()
    settings = source.model_copy(
        update={
            "database_name": source.test_sqlalchemy_url.database,
            "environment": "test",
            "model_max_tokens": 1024,
            "task_scheduler_enabled": False,
            "schedule_enabled": False,
            "knowledge_enabled": False,
        }
    )
    app = create_app(settings)
    user_id: UUID | None = None
    workspace_id: UUID | None = None
    try:
        async with app.router.lifespan_context(app):
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://real-chat-agent") as client:
                    username, password = "verify_" + uuid4().hex, secrets.token_urlsafe(24)
                    user_id, workspace_id, _ = await AccountManager(app.state.database.session_factory).create(
                        username, password, "对话协作临时验收", slug="verify-" + uuid4().hex, initialize=True
                    )
                    client.headers["Origin"] = settings.auth_trusted_origins[0]
                    login = await client.post("/api/auth/login", json={"username": username, "password": password})
                    login.raise_for_status()
                    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                    client.headers["X-Workspace-ID"] = str(workspace_id)
                    created = await client.post("/api/conversations", json={"title": "临时真实对话协作验收"})
                    created.raise_for_status()
                    conversation_id = created.json()["id"]
                    response = await client.post(
                        f"/api/conversations/{conversation_id}/runs",
                        json={
                            "content": "请在当前对话中完成：先创建工作计划，查询我最近7天的真实运行统计，"
                            "再把统计结果交给两个子Agent并行处理：analyst分析员总结数据，reviewer复核员检查解释的局限。"
                            "请确实调用update_plan、get_my_run_stats、delegate_tasks，最后更新计划并用100字内汇总。"
                            "两位子Agent各用60字内回答。不需要联网或文件操作。",
                            "idempotency_key": "real-chat-agent",
                        },
                    )
                    response.raise_for_status()
                    run_id = response.json()["run"]["id"]
                    async with asyncio.timeout(300):
                        while True:
                            response = await client.get(f"/api/runs/{run_id}")
                            response.raise_for_status()
                            snapshot = response.json()
                            if snapshot["run"]["status"] not in {"pending", "running"}:
                                break
                            await asyncio.sleep(1)
                    response = await client.get(f"/api/runs/{run_id}/tool-calls")
                    response.raise_for_status()
                    audits = response.json()
                    plans = [
                        row for row in audits if row["tool_name"] == "update_plan" and row["status"] == "completed"
                    ]
                    children = [row for row in audits if row["tool_name"] == "delegate_agent"]
                    tools = [row for row in audits if row["tool_name"] == "get_my_run_stats"]
                    statistics_valid = bool(tools) and all(
                        row["status"] == "completed" and json.loads(row["result"])["total"] == 1 for row in tools
                    )
                    verified = (
                        snapshot["run"]["status"] == "completed"
                        and bool(plans)
                        and len(children) == 2
                        and all(row["status"] == "completed" for row in children)
                        and statistics_valid
                    )
                    print(
                        json.dumps(
                            {
                                "verified": verified,
                                "model": settings.deepseek_model,
                                "run_status": snapshot["run"]["status"],
                                "error_code": snapshot["run"]["error_code"],
                                "plan_updates": len(plans),
                                "children": len(children),
                                "completed_children": sum(row["status"] == "completed" for row in children),
                                "statistics_verified": statistics_valid,
                                "model_actions": sum(row["tool_name"] == "model_request" for row in audits),
                                "result_characters": len(snapshot["message"]["content"]),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return verified
            finally:
                await app.state.chat_runtime.close()
                if user_id is not None and workspace_id is not None:
                    await cleanup(app.state.database, user_id, workspace_id)
    except Exception as exc:
        print(json.dumps({"verified": False, "error_type": type(exc).__name__}))
        return False


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
