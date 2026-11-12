"""真实测试数据库、真实 SDK 图与受控 HTTP 的工具权限验收。"""

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.agents.provider import DeepSeekProvider
from app.core.errors import ApplicationError
from app.models.identity import Workspace, WorkspaceMembership
from app.models.tool_call import ToolCall
from app.schemas.chat import RunCreate
from app.schemas.tools import TOOL_NAME, RunStatsArgs
from app.services.tools import ToolExecutor
from tests.auth_helpers import add_identity
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration


async def executor_for(app: FastAPI, client: httpx.AsyncClient) -> ToolExecutor:
    """创建由可信后端绑定的运行，暂不调度模型。

    Args:
        app: 测试应用。
        client: 已登录用户。

    Returns:
        绑定独立运行的工具入口。
    """
    conversation = await new_conversation(client)
    runtime = app.state.chat_runtime
    snapshot = await runtime.store.create_run(
        UUID(conversation), RunCreate(content="查询统计", idempotency_key="tool-test"), "test", app.state.test_scope
    )
    await runtime.store.begin_run(snapshot.run.id)
    return ToolExecutor(app.state.database.session_factory, runtime.settings, snapshot.run.id)


async def test_stats_isolation_and_private_audits(chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider]) -> None:
    """真实统计只包含本人当前空间，管理员也不能读取他人审计。

    Args:
        chat: 真实数据库和登录应用。
    """
    app, client, _ = chat
    first = await submit(client, await new_conversation(client))
    await settled(client, first["run"]["id"])
    executor = await executor_for(app, client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://other") as other:
        await add_identity(app, other, role="admin")
        foreign = await submit(other, await new_conversation(other))
        await settled(other, foreign["run"]["id"])
        result = json.loads(await executor.invoke(TOOL_NAME, {"days": 7}))
        assert result["ok"] and result["data"]["total"] == 2
        assert result["data"]["counts"]["completed"] == 1
        assert result["data"]["counts"]["running"] == 1
        path = f"/api/runs/{executor.run_id}/tool-calls"
        records = await client.get(path)
        assert records.status_code == 200
        row = records.json()[0]
        assert row["status"] == "completed" and row["days"] == 7
        assert "查询统计" not in records.text
        assert json.loads(row["result"])["total"] == 2
        assert (await other.get(path)).status_code == 404
        async with app.state.database.session_factory() as session:
            scope = app.state.test_scope
            space = await session.scalar(
                select(WorkspaceMembership.workspace_id).where(WorkspaceMembership.user_id == scope.user_id)
            )

            slug = await session.scalar(select(Workspace.slug).where(Workspace.id == space))
        await add_identity(app, other, workspace=slug, role="admin")
        assert (await other.get(path)).status_code == 404
        same_space = await submit(other, await new_conversation(other))
        await settled(other, same_space["run"]["id"])
        repeated = json.loads(await executor.invoke(TOOL_NAME, {"days": 7}))
        assert repeated["data"]["total"] == 2
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://anonymous") as anonymous:
            assert (await anonymous.get(path)).status_code == 401


@pytest.mark.parametrize(
    "arguments", [{"days": 0}, {"days": 91}, {"days": "7"}, {"user_id": "spoof"}, {"workspace_id": "spoof"}]
)
async def test_invalid_tool_arguments_are_audited_without_raw_input(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], arguments: dict[str, object]
) -> None:
    """模型身份伪造和越界参数不能进入查询或审计正文。

    Args:
        chat: 已登录应用。
        arguments: 不可信输入。
    """
    app, client, _ = chat
    executor = await executor_for(app, client)
    result = json.loads(await executor.invoke(TOOL_NAME, arguments))
    assert result["error_code"] == "TOOL_INVALID_ARGUMENTS"
    rows = (await client.get(f"/api/runs/{executor.run_id}/tool-calls")).json()
    assert rows[0]["status"] == "failed" and rows[0]["days"] is None
    assert rows[0]["result"] is None and "spoof" not in json.dumps(rows)


async def test_tool_limits_timeout_cancel_and_whitelist(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时与取消有终态，并行执行也受数据库计数限制。

    Args:
        chat: 已登录应用。
        monkeypatch: 替换慢查询边界。
    """
    app, client, _ = chat
    executor = await executor_for(app, client)
    executor.settings = executor.settings.model_copy(update={"tool_max_calls_per_run": 1})
    results = await asyncio.gather(executor.invoke(TOOL_NAME, {}), executor.invoke(TOOL_NAME, {}))
    decoded = [json.loads(item) for item in results]
    assert sum(item["ok"] for item in decoded) == 1
    assert any(item.get("error_code") == "TOOL_CALL_LIMIT" for item in decoded)
    with pytest.raises(ApplicationError, match="未获授权"):
        await executor.invoke("execute", {})
    executor.settings = executor.settings.model_copy(update={"tools_enabled": False})
    with pytest.raises(ApplicationError):
        await executor.invoke(TOOL_NAME, {})
    slow = await executor_for(app, client)
    slow.settings = slow.settings.model_copy(update={"tool_timeout_seconds": 0.03})
    entered = asyncio.Event()

    async def wait_query(arguments: RunStatsArgs) -> None:
        """保持调用未结束，验证超时与取消。

        Args:
            arguments: 已校验条件。
        """
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(slow, "_stats", wait_query)
    assert json.loads(await slow.invoke(TOOL_NAME, {}))["error_code"] == "TOOL_TIMEOUT"
    slow.settings = slow.settings.model_copy(update={"tool_timeout_seconds": 10})
    entered.clear()
    task = asyncio.create_task(slow.invoke(TOOL_NAME, {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = (await client.get(f"/api/runs/{slow.run_id}/tool-calls")).json()
    assert [row["status"] for row in rows] == ["failed", "cancelled"]


async def test_revoke_before_publishing_drops_tool_result(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """查询期间撤权，结果不能提交或发送给模型。

    Args:
        chat: 已登录应用。
        monkeypatch: 在查询完成后模拟撤权。
    """
    app, client, _ = chat
    executor = await executor_for(app, client)
    original = executor._stats

    async def revoke(arguments: RunStatsArgs) -> object:
        """在返回结果之前撤销成员。

        Args:
            arguments: 原查询条件。

        Returns:
            将被丢弃的聚合值。
        """
        result = await original(arguments)
        async with executor.factory.begin() as session:
            await session.execute(
                update(WorkspaceMembership)
                .where(WorkspaceMembership.user_id == app.state.test_scope.user_id)
                .values(status="revoked")
            )
        return result

    monkeypatch.setattr(executor, "_stats", revoke)
    with pytest.raises(ApplicationError, match="权限已失效"):
        await executor.invoke(TOOL_NAME, {})
    async with executor.factory() as session:
        row = await session.scalar(select(ToolCall).where(ToolCall.run_id == executor.run_id))
        assert row is not None and row.status == "failed" and row.result is None
    assert (await client.get(f"/api/runs/{executor.run_id}/tool-calls")).status_code == 403


@pytest.mark.parametrize("requested_tool", [TOOL_NAME, "task", "read_file"])
async def test_real_sdk_tool_roundtrip_and_builtin_denial(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch, requested_tool: str
) -> None:
    """真实 Deep Agents 图执行数据库工具，内置能力在执行前被拒绝。

    Args:
        chat: 真实数据库应用。
        monkeypatch: 只替换供应商 HTTP 传输。
        requested_tool: 模型尝试调用的工具。
    """
    from langchain_deepseek import ChatDeepSeek

    app, client, _ = chat
    captured: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """先返回工具调用，再依据真实工具结果返回正文。

        Args:
            request: SDK 请求。

        Returns:
            无外网的供应商 SSE。
        """
        body = json.loads(request.content)
        captured.append(body)
        assert {item["function"]["name"] for item in body["tools"]} == {TOOL_NAME, "update_plan", "delegate_tasks"}
        if len(captured) == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-stats",
                        "type": "function",
                        "function": {"name": requested_tool, "arguments": '{"days":7}'},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            result = json.loads(next(message["content"] for message in body["messages"] if message["role"] == "tool"))
            assert result["ok"] and result["data"]["total"] == 1
            delta, finish = {"content": "最近7天有1次运行（包含本次）。"}, "stop"
        payload = {
            "id": "tools-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return httpx.Response(
            200,
            text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as transport_client:

        def model(**kwargs: object) -> ChatDeepSeek:
            """使用真实 SDK 与受控传输。

            Args:
                kwargs: 真实模型配置。

            Returns:
                模型适配器。
            """
            return ChatDeepSeek(**kwargs, http_async_client=transport_client)

        monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
        runtime = app.state.chat_runtime
        runtime.settings = runtime.settings.model_copy(update={"deepseek_api_key": "test-only"})
        # model_copy 不执行 SecretStr 转换，明确构造有效类型。
        from pydantic import SecretStr

        runtime.settings.deepseek_api_key = SecretStr("test-only")
        runtime.provider = DeepSeekProvider(runtime.settings)
        snapshot = await submit(client, await new_conversation(client), "查一下我最近7天的运行统计")
        run_id = snapshot["run"]["id"]
        terminal = await settled(client, run_id)
        audits = [
            row
            for row in (await client.get(f"/api/runs/{run_id}/tool-calls")).json()
            if row["tool_name"] != "model_request"
        ]
        if requested_tool == TOOL_NAME:
            assert terminal["run"]["status"] == "completed", terminal
            assert terminal["message"]["content"] == "最近7天有1次运行（包含本次）。"
            assert len(captured) == 2 and audits[0]["status"] == "completed"
        else:
            assert terminal["run"]["error_code"] == "TOOL_NOT_ALLOWED", terminal
            assert len(captured) == 1 and not audits


async def test_failure_is_safe_and_restart_settles_audits(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """异常正文不进入结果，服务重启收敛遗留工具记录。

    Args:
        chat: 已登录测试应用。
        monkeypatch: 注入数据库能力失败。
    """
    app, client, _ = chat
    executor = await executor_for(app, client)

    async def broken(arguments: RunStatsArgs) -> None:
        """模拟不可公开的内部异常。

        Args:
            arguments: 合法参数。
        """
        raise RuntimeError("private-database-password")

    monkeypatch.setattr(executor, "_stats", broken)
    result = await executor.invoke(TOOL_NAME, {})
    assert json.loads(result)["error_code"] == "TOOL_EXECUTION_FAILED"
    assert "private" not in result
    async with executor.factory.begin() as session:
        session.add(ToolCall(run_id=executor.run_id, tool_name=TOOL_NAME))
    await app.state.chat_runtime.store.recover()
    rows = (await client.get(f"/api/runs/{executor.run_id}/tool-calls")).json()
    assert all(row["status"] == "failed" for row in rows)
    assert rows[1]["error_code"] == "PROCESS_RESTARTED"
    with pytest.raises(ApplicationError):
        await executor.invoke(TOOL_NAME, {})
