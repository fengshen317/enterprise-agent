"""在独立 PostgreSQL 中验证网页首个管理员只能创建一次。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.core.config import Settings
from app.main import create_app
from app.models.identity import User, Workspace
from app.models.system import FIRST_ADMIN_KEY, SystemMetadata
from tests.auth_helpers import ORIGIN, cleanup_identities

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


async def test_web_setup_creates_one_system_administrator(tmp_path: Path) -> None:
    """覆盖可信来源、口令验证、竞争提交、管理员登录和密码恢复。"""
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database
    database_env = {**os.environ, "EA_DATABASE_NAME": target.database}
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=database_env,
        check=True,
    )
    token_file = tmp_path / "setup-token"
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "environment": "test",
            "system_admin_username": "",
            "initial_setup_token": SecretStr(""),
            "initial_setup_token_file": token_file,
            "task_scheduler_enabled": False,
            "schedule_enabled": False,
            "connector_monitor_enabled": False,
            "knowledge_enabled": False,
            "channel_enabled": False,
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        factory = app.state.database.session_factory
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            path = "/api/auth/setup"
            state = await client.get(path)
            assert state.status_code == 200
            if not state.json()["available"]:
                pytest.skip("需要无账号和空间的测试库")
            token = token_file.read_text(encoding="utf-8").strip()
            assert state.json() == {"available": True, "username": None}
            assert state.headers["Cache-Control"] == "no-store" and token not in state.text
            names = ["setup-" + uuid4().hex for _ in range(2)]
            payload = {
                "username": names[0],
                "display_name": "部署管理员",
                "password": "短密码1",
                "token": token,
            }
            headers = {"Origin": ORIGIN}
            try:
                rejected_origin = await client.post(path, json=payload, headers={"Origin": "https://foreign.invalid"})
                assert rejected_origin.status_code == 403
                rejected_token = await client.post(path, json={**payload, "token": "x" * 43}, headers=headers)
                assert rejected_token.status_code == 403
                for short_password in ("", "abc", "🔑🔒"):
                    rejected_password = await client.post(
                        path, json={**payload, "password": short_password}, headers=headers
                    )
                    assert rejected_password.status_code == 422
                    assert "PASSWORD_TOO_SHORT" in rejected_password.text
                assert token_file.is_file()
                first, second = await asyncio.gather(
                    client.post(path, json=payload, headers=headers),
                    client.post(path, json={**payload, "username": names[1]}, headers=headers),
                )
                assert sorted((first.status_code, second.status_code)) == [201, 409]
                created = first if first.status_code == 201 else second
                username = created.json()["username"]
                assert created.json() == {"username": username} and token not in created.text
                assert not token_file.exists()
                assert (await client.get(path)).json() == {"available": False, "username": None}
                assert (await client.post(path, json=payload, headers=headers)).status_code == 409
                login = await client.post(
                    "/api/auth/login",
                    json={"username": username, "password": payload["password"]},
                    headers=headers,
                )
                assert login.status_code == 200
                assert login.json()["system_admin"] is True
                assert login.json()["workspaces"][0]["role"] == "admin"
                assert (await client.get("/api/admin/configs")).status_code == 200
                command = [sys.executable, "-m", "app.cli.accounts", "reset-password", "--username", username]
                for short_password in ("", "123"):
                    rejected_reset = await asyncio.to_thread(
                        subprocess.run,
                        command,
                        cwd=BACKEND_DIR,
                        env=database_env,
                        input=f"{short_password}\n{short_password}\n",
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    assert rejected_reset.returncode == 1
                    assert "PASSWORD_TOO_SHORT" in rejected_reset.stdout
                assert (await client.get("/api/auth/me")).status_code == 200
                assert (await client.post(
                    "/api/auth/login", json={"username": username, "password": payload["password"]}, headers=headers
                )).status_code == 200
                new_password = "x" * 257
                await asyncio.to_thread(
                    subprocess.run,
                    command,
                    cwd=BACKEND_DIR,
                    env=database_env,
                    input=f"{new_password}\n{new_password}\n",
                    text=True,
                    check=True,
                )
                assert (await client.get("/api/auth/me")).status_code == 401
                assert (await client.post(
                    "/api/auth/login", json={"username": username, "password": payload["password"]}, headers=headers
                )).status_code == 401
                restored = await client.post(
                    "/api/auth/login", json={"username": username, "password": new_password}, headers=headers
                )
                assert restored.status_code == 200 and restored.json()["system_admin"] is True
            finally:
                async with factory.begin() as session:
                    ids = list(await session.scalars(select(User.id).where(User.username.in_(names))))
                    app.state.test_users = ids
                    app.state.test_spaces = list(
                        await session.scalars(select(Workspace.id).where(Workspace.created_by_user_id.in_(ids)))
                    )
                    await session.execute(
                        delete(SystemMetadata).where(
                            SystemMetadata.key == FIRST_ADMIN_KEY, SystemMetadata.value.in_(tuple(map(str, ids)))
                        )
                    )
                await cleanup_identities(app)
