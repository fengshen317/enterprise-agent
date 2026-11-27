"""验证个人连接器从工作台安装、成员绑定到 Agent 调用时的隔离。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.connectors.personal import ADAPTERS
from app.core.errors import ApplicationError
from app.models.connector import Connector, PersonalConnectorBinding
from app.models.identity import Workspace, WorkspaceMembership
from app.schemas.auth import ResourceScope
from app.schemas.chat import RunCreate
from app.services.tools import ToolExecutor
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation
from tests.test_chat_integration import chat as chat


@pytest.mark.integration
@pytest.mark.parametrize(
    ("key", "values"),
    [
        ("qq_mail", {"email": "tester@example.org", "auth_code": "private-imap-test"}),
        ("ima", {"client_id": "test-client", "api_key": "private-ima-test"}),
        ("tencent_news", {"api_key": "private-news-test"}),
        ("tavily_search", {"api_key": "private-tavily-test"}),
    ],
)
async def test_member_binding_lifecycle(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    values: dict[str, str],
) -> None:
    """真实数据库验证隔离、密文、撤销和旧工具执行前复核。"""
    app, admin, _ = chat
    factory = app.state.database.session_factory
    assembler = app.state.chat_runtime.assembler
    owner = app.state.test_scope
    settings = app.state.settings
    monkeypatch.setattr(settings, "connector_credential_key", SecretStr(Fernet.generate_key().decode()))
    adapter = AsyncMock(return_value={"private": "test-content"})
    definitions = ADAPTERS[key][0]
    monkeypatch.setitem(ADAPTERS, key, (definitions, adapter))
    async with factory() as session:
        assert not await session.scalar(select(Connector.id).where(Connector.key == key))
        slug = await session.scalar(select(Workspace.slug).where(Workspace.id == owner.workspace_id))
    path = f"/api/connectors/{key}"
    manage = f"/api/admin/connectors/{key}"
    try:
        assert (await admin.post(manage + "/install")).is_success
        assert (await admin.post(manage + "/enabled", json={"enabled": True})).is_success
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as member:
            scope = await add_identity(app, member, workspace=slug)
            assert (await member.get(path)).json()["personal_account"]["bound"] is False
            assert (
                await member.put(path + "/my-account", json={"credentials": {}, "user_id": str(owner.user_id)})
            ).status_code == 422
            binding = await member.put(path + "/my-account", json={"credentials": values})
            assert binding.status_code == 200 and binding.json()["bound"] is True
            assert binding.headers["cache-control"] == "no-store"
            assert (await admin.get(path)).json()["personal_account"]["bound"] is False
            async with factory() as session:
                ciphertext = await session.scalar(
                    select(PersonalConnectorBinding.ciphertext).filter_by(
                        workspace_id=scope.workspace_id, user_id=scope.user_id, connector_key=key
                    )
                )
            assert ciphertext and all(
                value not in ciphertext and value not in binding.text for value in values.values()
            )
            assembly = await assembler.assemble(scope, None)
            specs = [item for item in assembly.tools if item.connector_key == key]
            specs = [item for item in specs if item.source == "connector"]
            assert len(specs) == len(definitions)
            assert not {spec.key for spec in specs} & (await assembler.assemble(owner, None)).tool_keys
            context = SimpleNamespace(factory=factory, scope=scope, settings=settings)
            # 使用真实 Run 和审计接口，只替换会访问个人数据的外部适配器。
            spec = specs[0]
            raw_args = {"query": "科技"} if key in {"tencent_news", "tavily_search"} else {}
            args = spec.args_model.model_validate(raw_args)
            runtime = app.state.chat_runtime
            run = await runtime.create_run(
                UUID(await new_conversation(member)), RunCreate(content="读取本人数据", idempotency_key=key), scope
            )
            await runtime.store.begin_run(run.run.id)
            executor = ToolExecutor(
                factory, settings, run.run.id, assembly=await assembler.assemble_for_run(run.run.id)
            )
            result = json.loads(await executor.invoke(spec.key, raw_args))
            assert result["ok"] and result["data"] == {"private": "test-content"}
            assert adapter.await_args.args[2] == values
            audit = await member.get(f"/api/runs/{run.run.id}/tool-calls")
            assert json.loads(audit.json()[0]["result"]) == {"action": definitions[0].name}
            assert "test-content" not in audit.text
            other_workspace = ResourceScope(scope.user_id, (await add_identity(app, admin)).workspace_id)
            async with factory.begin() as session:
                session.add(WorkspaceMembership(workspace_id=other_workspace.workspace_id, user_id=scope.user_id))
            assert spec.key not in (await assembler.assemble(other_workspace, None)).tool_keys
            with pytest.raises(ApplicationError, match="绑定"):
                await spec.handler(SimpleNamespace(factory=factory, scope=other_workspace, settings=settings), args)
            rotated = {**values, ("auth_code" if key == "qq_mail" else "api_key"): "replacement-test"}
            assert (await member.put(path + "/my-account", json={"credentials": rotated})).json()["bound"]
            await spec.handler(context, args)
            assert adapter.await_args.args[2] == rotated
            assert (await admin.post(manage + "/enabled", json={"enabled": False})).is_success
            with pytest.raises(ApplicationError, match="绑定"):
                await spec.handler(context, args)
            assert (await member.delete(path + "/my-account")).json()["bound"] is False
            assert (await admin.post(manage + "/enabled", json={"enabled": True})).is_success
            async with factory() as session:
                assert spec.key not in (await assembler._restore(session, scope, assembly.snapshot)).tool_keys
            denied = json.loads(await executor.invoke(spec.key, raw_args))
            assert not denied["ok"] and denied["error_code"] == "PERSONAL_CONNECTOR_UNBOUND"
            assert adapter.await_count == 2
    finally:
        async with factory.begin() as session:
            await session.execute(delete(Connector).where(Connector.key == key))
        await app.state.connector_registry.refresh(key)
