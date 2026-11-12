"""显式真实模型验收：两次短调用，只使用专用测试库并清理本次数据。"""

import argparse
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


async def main(compression: bool = False) -> bool:
    """验证真实模型追问及可选持久化压缩，不打印密钥或异常正文。

    Args:
        compression: 是否降低阈值并验证真实摘要。

    Returns:
        是否通过全部验收条件。
    """
    source = Settings()
    target = source.test_sqlalchemy_url
    if not source.deepseek_api_key.get_secret_value().strip():
        print(json.dumps({"verified": False, "reason": "MODEL_NOT_CONFIGURED"}))
        return False
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "model_max_tokens": 128,
            "schedule_enabled": False,
            "knowledge_enabled": False,
        }
    )
    if compression:
        settings = settings.model_copy(
            update={
                "model_context_characters": 1000,
                "context_compression_enabled": True,
                "context_compression_trigger_ratio": 0.7,
            }
        )
    app = create_app(settings)
    conversation_id: UUID | None = None
    user_id: UUID | None = None
    workspace_id: UUID | None = None
    nonce = "SEQ" + uuid4().hex[:8]
    results: list[dict[str, object]] = []
    try:
        async with app.router.lifespan_context(app):
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://real-check") as client:
                    username = "verify_" + uuid4().hex
                    password = secrets.token_urlsafe(24)
                    user_id, workspace_id, _ = await AccountManager(app.state.database.session_factory).create(
                        username, password, "临时验收用户", slug="verify-" + uuid4().hex, initialize=True
                    )
                    client.headers["Origin"] = settings.auth_trusted_origins[0]
                    login = await client.post("/api/auth/login", json={"username": username, "password": password})
                    login.raise_for_status()
                    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                    client.headers["X-Workspace-ID"] = str(workspace_id)
                    response = await client.post("/api/conversations", json={"title": "真实模型临时验收"})
                    response.raise_for_status()
                    conversation_id = UUID(response.json()["id"])
                    questions = [f"请记住验收代号 {nonce}，只回答收到。", "刚才的验收代号是什么？只回答代号。"]
                    if compression:
                        questions[0] = "这是本次验收的背景材料。" * 60 + questions[0]
                    for index, question in enumerate(questions):
                        response = await client.post(
                            f"/api/conversations/{conversation_id}/runs",
                            json={"content": question, "idempotency_key": f"verify-{index}"},
                        )
                        response.raise_for_status()
                        run_id = response.json()["run"]["id"]
                        async with asyncio.timeout(settings.model_timeout_seconds + 10):
                            while True:
                                snapshot = (await client.get(f"/api/runs/{run_id}")).json()
                                if snapshot["run"]["status"] not in ("pending", "running"):
                                    break
                                await asyncio.sleep(0.5)
                        results.append(
                            {
                                "turn": index + 1,
                                "status": snapshot["run"]["status"],
                                "error_code": snapshot["run"]["error_code"],
                                "answer_characters": len(snapshot["message"]["content"]),
                            }
                        )
                        if snapshot["run"]["status"] != "completed":
                            break
                    remembered = (
                        len(results) == 2
                        and all(item["status"] == "completed" for item in results)
                        and nonce in snapshot["message"]["content"]
                    )
                    async with app.state.database.session_factory() as session:
                        row = await session.get(Conversation, conversation_id)
                        compressed = row.summary_version > 0 and row.summary_through_sequence >= 2
                    verified = remembered and (not compression or compressed)
                    history = (await client.get(f"/api/conversations/{conversation_id}/messages")).json()
                    print(
                        json.dumps(
                            {
                                "verified": verified,
                                "authenticated": True,
                                "compression_verified": compressed if compression else None,
                                "model": settings.deepseek_model,
                                "turns": results,
                                "persisted_messages": len(history["items"]),
                                "context_remembered": remembered,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return verified
            finally:
                await app.state.chat_runtime.close()
                if conversation_id is not None:
                    async with app.state.database.session_factory.begin() as session:
                        await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
                        await session.execute(delete(Run).where(Run.conversation_id == conversation_id))
                        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
                if user_id is not None:
                    async with app.state.database.session_factory.begin() as session:
                        await session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
                        await session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.user_id == user_id))
                        await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
                        await session.execute(delete(User).where(User.id == user_id))
    except Exception as exc:
        print(json.dumps({"verified": False, "error_type": type(exc).__name__}, ensure_ascii=False))
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实问答验收，会调用 DeepSeek API")
    parser.add_argument("--compression", action="store_true", help="增加真实摘要压缩验收")
    arguments = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(arguments.compression)) else 1)
