"""真实工具验收：临时测试账号、真实模型和真实 PostgreSQL，结束后精确清理。"""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.config import Settings
from app.main import create_app
from app.models.conversation import Conversation, Message, Run
from app.models.identity import AuthSession, User, Workspace, WorkspaceMembership
from app.services.accounts import AccountManager


async def main() -> bool:
    """通过真实登录与问答入口验证模型主动调用只读工具。

    Returns:
        是否完成真实工具调用、持久化审计和最终回答。
    """
    source = Settings()
    target = source.test_sqlalchemy_url
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "model_max_tokens": 512,
            "tools_enabled": True,
            "schedule_enabled": False,
            "knowledge_enabled": False,
        }
    )
    app = create_app(settings)
    user_id: UUID | None = None
    workspace_id: UUID | None = None
    conversation_id: UUID | None = None
    try:
        async with app.router.lifespan_context(app):
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://real-tools") as client:
                    username, password = "verify_" + uuid4().hex, secrets.token_urlsafe(24)
                    user_id, workspace_id, _ = await AccountManager(app.state.database.session_factory).create(
                        username, password, "工具临时验收", slug="verify-" + uuid4().hex, initialize=True
                    )
                    client.headers["Origin"] = settings.auth_trusted_origins[0]
                    login = await client.post("/api/auth/login", json={"username": username, "password": password})
                    login.raise_for_status()
                    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                    client.headers["X-Workspace-ID"] = str(workspace_id)
                    response = await client.post("/api/conversations", json={"title": "真实工具临时验收"})
                    response.raise_for_status()
                    conversation_id = UUID(response.json()["id"])
                    response = await client.post(
                        f"/api/conversations/{conversation_id}/runs",
                        json={
                            "content": "请调用 get_my_run_stats 查询我最近7天的运行统计，简短回答。",
                            "idempotency_key": "real-tool-check",
                        },
                    )
                    response.raise_for_status()
                    run_id = response.json()["run"]["id"]
                    async with asyncio.timeout(settings.model_timeout_seconds + 10):
                        while True:
                            response = await client.get(f"/api/runs/{run_id}")
                            response.raise_for_status()
                            snapshot = response.json()
                            if snapshot["run"]["status"] not in ("pending", "running"):
                                break
                            await asyncio.sleep(0.5)
                    response = await client.get(f"/api/runs/{run_id}/tool-calls")
                    response.raise_for_status()
                    calls = response.json()
                    success = [row for row in calls if row["status"] == "completed"]
                    correct = bool(success) and all(json.loads(row["result"])["total"] == 1 for row in success)
                    verified = snapshot["run"]["status"] == "completed" and correct
                    print(
                        json.dumps(
                            {
                                "verified": verified,
                                "authenticated": True,
                                "model": settings.deepseek_model,
                                "run_status": snapshot["run"]["status"],
                                "error_code": snapshot["run"]["error_code"],
                                "tool_calls": len(calls),
                                "successful_tool_calls": len(success),
                                "stats_verified": correct,
                                "answer_characters": len(snapshot["message"]["content"]),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return verified
            finally:
                await app.state.chat_runtime.close()
                async with app.state.database.session_factory.begin() as session:
                    if conversation_id is not None:
                        await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
                        await session.execute(delete(Run).where(Run.conversation_id == conversation_id))
                        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
                    if user_id is not None:
                        await session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
                        await session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.user_id == user_id))
                        await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
                        await session.execute(delete(User).where(User.id == user_id))
    except Exception as exc:
        print(json.dumps({"verified": False, "error_type": type(exc).__name__}))
        return False


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
