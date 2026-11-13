"""真实数据库与真实 Deep Agents 图验证对话内规划、并行委派及重试。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.agents.provider import DeepSeekProvider
from app.schemas.chat import RunCreate
from app.schemas.chat_agent import DelegateArgs, PlanArgs
from app.services.chat_agent import ChatAgentExecutor
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat
from tests.test_tools_integration import executor_for

pytestmark = pytest.mark.integration


def response(content: str = "", name: str | None = None, arguments: dict[str, Any] | None = None) -> httpx.Response:
    """构造供应商 SSE，不伪造 Agent 图。

    Args:
        content: 模型正文。
        name: 工具名称。
        arguments: 工具参数。

    Returns:
        SDK 可读取的传输响应。
    """
    delta: dict[str, Any] = {"content": content}
    if name:
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-" + name,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
            ]
        }
    payload = {
        "id": "chat-agent",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if name else "stop"}],
    }
    return httpx.Response(
        200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n", headers={"Content-Type": "text/event-stream"}
    )


async def test_chat_plan_parallel_agents_retry_and_private_history(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一问答内规划并行委派；模型重试不重放子任务，刷新保留真实审计。

    Args:
        chat: 真实数据库应用。
        monkeypatch: 仅替换 HTTP 传输。
    """
    from langchain_deepseek import ChatDeepSeek

    app, client, _ = chat
    runtime = app.state.chat_runtime
    runtime.settings = runtime.settings.model_copy(
        update={
            "deepseek_api_key": SecretStr("test-only"),
            "task_retry_base_seconds": 0.001,
            "task_retry_max_seconds": 0.001,
        }
    )
    runtime.provider = DeepSeekProvider(runtime.settings)
    child_started = 0
    main_requests = 0
    together = asyncio.Event()
    plan = {"steps": [{"title": "独立分析与复核", "status": "in_progress"}]}

    async def transport(request: httpx.Request) -> httpx.Response:
        """通过真实图的请求区分主 Agent 和独立子 Agent。

        Args:
            request: 模型真实 HTTP 请求。

        Returns:
            可控 SSE 或一次瞬时限流。
        """
        nonlocal child_started, main_requests
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        system = str(body["messages"][0]["content"])
        if "独立的" in system:
            assert names == {"get_my_run_stats"}
            tool_messages = [message for message in body["messages"] if message["role"] == "tool"]
            if not tool_messages:
                assert len(body["messages"]) == 2
                child_started += 1
                if child_started == 2:
                    together.set()
                await asyncio.wait_for(together.wait(), 3)
                return response(name="get_my_run_stats", arguments={"days": 7})
            result = json.loads(tool_messages[-1]["content"])
            assert result["ok"] and result["data"]["total"] == 1
            return response("子任务真实图结果")
        assert names == {"get_my_run_stats", "update_plan", "delegate_tasks"}
        main_requests += 1
        if main_requests == 1:
            return response(name="update_plan", arguments=plan)
        if main_requests == 2:
            return response(
                name="delegate_tasks",
                arguments={
                    "tasks": [
                        {"role": "analyst", "title": "分析方案", "instruction": "分析用户给出的目标A"},
                        {"role": "reviewer", "title": "独立复核", "instruction": "复核用户给出的目标A"},
                    ]
                },
            )
        if main_requests == 3:
            return httpx.Response(429, json={"error": {"code": "rate_limit"}}, headers={"Retry-After": "0"})
        if main_requests == 4:
            plan["steps"][0]["status"] = "completed"
            return response(name="update_plan", arguments=plan)
        return response("已根据两个子 Agent 的结果汇总。")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)):

        def model(**kwargs: Any) -> ChatDeepSeek:
            """给真实供应商适配器注入传输。

            Args:
                kwargs: 模型配置。

            Returns:
                真实模型适配器。
            """
            return ChatDeepSeek(**kwargs, http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

        monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
        conversation = await new_conversation(client)
        snapshot = await submit(client, conversation, "制定计划并让分析员与复核员独立分析")
        terminal = await settled(client, snapshot["run"]["id"])
    assert terminal["run"]["status"] == "completed", json.dumps(terminal, ensure_ascii=False)
    assert terminal["message"]["content"] == "已根据两个子 Agent 的结果汇总。"
    assert child_started == 2 and main_requests == 5
    path = f"/api/runs/{snapshot['run']['id']}/tool-calls"
    audits = (await client.get(path)).json()
    children = [row for row in audits if row["tool_name"] == "delegate_agent"]
    assert len(children) == 2 and all(row["status"] == "completed" for row in children)
    plans = [row for row in audits if row["tool_name"] == "update_plan"]
    assert len(plans) == 2 and json.loads(plans[-1]["result"])["steps"][0]["status"] == "completed"
    assert (await client.get(path)).json() == audits
    assert "子任务真实图结果" in json.dumps(audits, ensure_ascii=False)
    follow = await app.state.chat_runtime.store.create_run(
        UUID(conversation), RunCreate(content="补充复核", idempotency_key="follow"), "test", app.state.test_scope
    )
    background = await app.state.chat_runtime.store.agent_context(follow.run.id)
    assert "子任务真实图结果" in background and "制定计划" in background


async def test_cancel_delegation_settles_all_children(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消委派必须清理两条正在运行的子 Agent。

    Args:
        chat: 已登录数据库应用。
        monkeypatch: 替换子 Agent 模型边界。
    """
    app, client, _ = chat
    base = await executor_for(app, client)
    executor = ChatAgentExecutor(base.factory, base.settings, base.run_id)
    started = 0
    both = asyncio.Event()

    async def blocked(
        self: DeepSeekProvider, history: list[tuple[str, str]], *, model: str | None = None
    ) -> AsyncIterator[str]:
        """保持子图运行以验证取消传播。

        Args:
            self: 独立适配器。
            history: 子任务上下文。

        Yields:
            不会发送的正文。
        """
        del model
        nonlocal started
        started += 1
        if started == 2:
            both.set()
        await asyncio.Event().wait()
        yield "不会返回"

    monkeypatch.setattr(DeepSeekProvider, "stream", blocked)
    task = asyncio.create_task(
        executor.invoke(
            "delegate_tasks",
            {
                "tasks": [
                    {"role": "analyst", "title": "A", "instruction": "A"},
                    {"role": "reviewer", "title": "B", "instruction": "B"},
                ]
            },
        )
    )
    await asyncio.wait_for(both.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    audits = (await client.get(f"/api/runs/{base.run_id}/tool-calls")).json()
    assert len(audits) == 3 and all(row["status"] == "cancelled" for row in audits)


@pytest.mark.parametrize(
    "schema,body",
    [
        (PlanArgs, {"steps": []}),
        (PlanArgs, {"steps": [{"title": "步骤", "status": "unknown"}]}),
        (DelegateArgs, {"tasks": [{"role": "admin", "title": "越权", "instruction": "执行"}]}),
        (DelegateArgs, {"tasks": [], "user_id": "spoof"}),
    ],
)
def test_agent_inputs_are_bounded(schema: Any, body: dict[str, Any]) -> None:
    """身份、角色和计划状态不能由模型任意扩展。

    Args:
        schema: 输入模型。
        body: 不合法输入。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        schema.model_validate(body)


@pytest.mark.parametrize(
    "kind,expected,attempts",
    [
        ("network", "NETWORK_UNAVAILABLE", 3),
        ("auth", "MODEL_AUTH_DENIED", 1),
        ("long_wait", "RETRY_WAIT_TOO_LONG", 1),
    ],
)
async def test_model_retry_bounds_and_safe_journal(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], kind: str, expected: str, attempts: int
) -> None:
    """瞬断有限重试，鉴权与长等待不盲重试，也不保存供应商私密正文。

    Args:
        chat: 测试数据库应用。
        kind: 注入异常类别。
        expected: 安全错误码。
        attempts: 预期实际调用次数。
    """
    from app.core.errors import ApplicationError

    app, client, _ = chat
    base = await executor_for(app, client)
    settings = base.settings.model_copy(update={"task_retry_base_seconds": 0.001, "task_retry_max_seconds": 0.001})
    executor = ChatAgentExecutor(base.factory, settings, base.run_id)
    requests = 0

    async def broken() -> str:
        """注入异常并记录真实请求次数。

        Returns:
            不会返回的正文。
        """
        nonlocal requests
        requests += 1
        request = httpx.Request("POST", "https://example.invalid")
        if kind == "network":
            raise httpx.ConnectError("private-key-provider-body", request=request)
        response = httpx.Response(
            401 if kind == "auth" else 429,
            request=request,
            json={"error": "private-key-provider-body"},
            headers={"Retry-After": "999"},
        )
        raise httpx.HTTPStatusError("private-key-provider-body", request=request, response=response)

    with pytest.raises(ApplicationError) as found:
        await executor.model_call(broken)
    assert found.value.code == expected
    assert requests == attempts
    audits = (await client.get(f"/api/runs/{base.run_id}/tool-calls")).json()
    assert len(audits) == 1 and audits[0]["status"] == "failed"
    assert audits[0]["error_code"] == expected
    assert "private-key" not in json.dumps(audits)


async def test_plan_validation_and_delegation_quota(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """无效计划留下失败审计，超额子任务不会触发额外模型调用。

    Args:
        chat: 已登录数据库应用。
        monkeypatch: 替换子模型输出边界。
    """
    app, client, _ = chat
    base = await executor_for(app, client)
    executor = ChatAgentExecutor(base.factory, base.settings, base.run_id)
    invalid = json.loads(await executor.invoke("update_plan", {"steps": [], "user_id": "spoof"}))
    assert invalid["error_code"] == "TOOL_INVALID_ARGUMENTS"
    calls = 0

    async def generated(
        self: DeepSeekProvider, history: list[tuple[str, str]], *, model: str | None = None
    ) -> AsyncIterator[str]:
        """只统计实际进入子模型的次数。

        Args:
            self: 模型适配器。
            history: 独立上下文。
            model: 本次运行模型。

        Yields:
            最小测试结果。
        """
        del history, model
        nonlocal calls
        calls += 1
        yield "已分析"

    monkeypatch.setattr(DeepSeekProvider, "stream", generated)
    task = {"role": "analyst", "title": "分析", "instruction": "独立分析"}
    for _ in range(4):
        await executor.invoke("delegate_tasks", {"tasks": [task, task]})
    assert calls == 6
    audits = (await client.get(f"/api/runs/{base.run_id}/tool-calls")).json()
    assert any(row["error_code"] == "DELEGATION_LIMIT" for row in audits)
    assert "spoof" not in json.dumps(audits)
