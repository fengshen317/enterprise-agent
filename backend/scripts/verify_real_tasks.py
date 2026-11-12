"""独立真实模型任务验收，使用专用测试库并按本次标识清理。"""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import Settings
from app.main import create_app
from app.models.identity import AuthSession, User, Workspace, WorkspaceMembership
from app.models.task import Task, TaskAction
from app.services.accounts import AccountManager


async def main() -> bool:
    """验证真实规划、用户确认、只读工具与模型汇总的完整闭环。

    Returns:
        是否通过完整真实任务验收。
    """
    source = Settings()
    target = source.test_sqlalchemy_url
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "environment": "test",
            "model_max_tokens": 1024,
            "task_scheduler_enabled": True,
            "schedule_enabled": False,
            "knowledge_enabled": False,
            "task_parallel_steps": 1,
            "task_poll_seconds": 0.1,
        }
    )
    app = create_app(settings)
    user_id: UUID | None = None
    workspace_id: UUID | None = None
    task_id: UUID | None = None
    try:
        async with app.router.lifespan_context(app):
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://real-task") as client:
                    username, password = "verify_" + uuid4().hex, secrets.token_urlsafe(24)
                    user_id, workspace_id, _ = await AccountManager(app.state.database.session_factory).create(
                        username, password, "任务临时验收", slug="verify-" + uuid4().hex, initialize=True
                    )
                    client.headers["Origin"] = settings.auth_trusted_origins[0]
                    login = await client.post("/api/auth/login", json={"username": username, "password": password})
                    login.raise_for_status()
                    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                    client.headers["X-Workspace-ID"] = str(workspace_id)
                    response = await client.post(
                        "/api/tasks",
                        json={
                            "goal": "请查询我当前空间最近7天的问答运行数量并简短总结。"
                            "计划只需2步：get_my_run_stats工具查询，以及依赖查询结果的synthesis汇总。",
                            "idempotency_key": "real-task-verification",
                        },
                    )
                    response.raise_for_status()
                    task_id = UUID(response.json()["task"]["id"])
                    async with asyncio.timeout(240):
                        while True:
                            response = await client.get(f"/api/tasks/{task_id}")
                            response.raise_for_status()
                            snapshot = response.json()
                            if snapshot["run"]["status"] in ("awaiting_confirmation", "failed", "cancelled"):
                                break
                            await asyncio.sleep(0.5)
                        planned = snapshot["run"]["status"] == "awaiting_confirmation"
                        if planned:
                            response = await client.post(
                                f"/api/tasks/{task_id}/confirm",
                                json={
                                    "run_id": snapshot["run"]["id"],
                                    "revision": snapshot["plan"]["revision"],
                                    "digest": snapshot["plan"]["digest"],
                                },
                            )
                            response.raise_for_status()
                            while True:
                                response = await client.get(f"/api/tasks/{task_id}")
                                response.raise_for_status()
                                snapshot = response.json()
                                if snapshot["run"]["status"] in ("completed", "failed", "cancelled", "paused"):
                                    break
                                await asyncio.sleep(0.5)
                    async with app.state.database.session_factory() as session:
                        tool_results = list(
                            await session.scalars(
                                select(TaskAction.result).where(
                                    TaskAction.run_id == UUID(snapshot["run"]["id"]),
                                    TaskAction.kind == "tool",
                                    TaskAction.status == "succeeded",
                                )
                            )
                        )
                    stats_valid = bool(tool_results) and all(
                        json.loads(result or "{}").get("total") == 0 for result in tool_results
                    )
                    verified = planned and snapshot["run"]["status"] == "completed" and stats_valid
                    print(
                        json.dumps(
                            {
                                "verified": verified,
                                "authenticated": True,
                                "plan_confirmed": planned,
                                "model": settings.deepseek_model,
                                "run_status": snapshot["run"]["status"],
                                "error_code": snapshot["run"]["error_code"],
                                "steps": len(snapshot["steps"]),
                                "completed_steps": sum(item["status"] == "succeeded" for item in snapshot["steps"]),
                                "tool_queries": len(tool_results),
                                "tool_stats_verified": stats_valid,
                                "model_requests": sum(
                                    item["attempt_count"]
                                    for item in snapshot["actions"]
                                    if item["kind"] in ("plan", "model")
                                ),
                                "result_characters": len(snapshot["run"]["result"] or ""),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return verified
            finally:
                await app.state.task_runtime.close()
                async with app.state.database.session_factory.begin() as session:
                    if task_id is not None:
                        await session.execute(delete(Task).where(Task.id == task_id))
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
