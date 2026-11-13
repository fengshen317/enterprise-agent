"""真实 Deep Agents 图和受控 HTTP 的规划/步骤生成边界。"""

import json

import httpx
import pytest

from app.agents.task_provider import DeepSeekTaskProvider
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.task import PlanStep


@pytest.mark.parametrize("operation", ["plan", "execute"])
async def test_task_provider_real_sdk_uses_only_complete_results(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """计划和分析都禁用 SDK 内置工具，完整结果才进入持久化边界。

    Args:
        monkeypatch: 只替换供应商 HTTP 传输。
        operation: 规划或步骤生成。
    """
    from langchain_deepseek import ChatDeepSeek

    captured: list[dict] = []
    plan = {"steps": [{"key": "summary", "title": "总结", "kind": "synthesis"}]}

    def transport(request: httpx.Request) -> httpx.Response:
        """返回无网络的完整供应商响应。

        Args:
            request: SDK 的 HTTP 请求。

        Returns:
            真实 SDK 可读取的供应商格式。
        """
        body = json.loads(request.content)
        captured.append(body)
        assert not body.get("tools")
        assert not body.get("stream")
        assert body["thinking"] == {"type": "disabled"}
        content = json.dumps(plan) if operation == "plan" else "只基于真实输入给出的总结。"
        return httpx.Response(
            200,
            json={
                "id": "task-test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def model(**kwargs: object) -> ChatDeepSeek:
        """给真实模型适配器注入受控传输。

        Args:
            kwargs: 模型初始化参数。

        Returns:
            带测试 HTTP 的真实适配器。
        """
        return ChatDeepSeek(**kwargs, http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
    provider = DeepSeekTaskProvider(Settings(_env_file=None, deepseek_api_key="test-only"))
    if operation == "plan":
        assert (await provider.plan("解释一个概念")).steps[0].key == "summary"
    else:
        result = await provider.execute("解释一个概念", PlanStep(key="summary", title="总结", kind="synthesis"), {})
        assert result == "只基于真实输入给出的总结。"
    assert len(captured) == 1


@pytest.mark.parametrize(
    "mode,code", [("tool", "TOOL_NOT_ALLOWED"), ("truncated", "MODEL_OUTPUT_TRUNCATED"), ("invalid", "INVALID_PLAN")]
)
async def test_task_provider_rejects_incomplete_plan_and_hidden_tools(
    monkeypatch: pytest.MonkeyPatch, mode: str, code: str
) -> None:
    """模型不能在计划阶段执行工具，不完整内容也不能伪装成成功。

    Args:
        monkeypatch: 替换供应商 HTTP 传输。
        mode: 拒绝场景。
        code: 预期公开错误。
    """
    from langchain_deepseek import ChatDeepSeek

    def transport(request: httpx.Request) -> httpx.Response:
        """构造被拒绝的模型结果。

        Args:
            request: SDK 请求。

        Returns:
            供应商格式响应。
        """
        message: dict[str, object] = {"role": "assistant", "content": "not-json"}
        finish = "length" if mode == "truncated" else "stop"
        if mode == "tool":
            message["content"] = ""
            message["tool_calls"] = [
                {"id": "unsafe", "type": "function", "function": {"name": "task", "arguments": "{}"}}
            ]
            finish = "tool_calls"
        return httpx.Response(
            200,
            json={
                "id": "task-test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def model(**kwargs: object) -> ChatDeepSeek:
        """使用真实 SDK 解析失败结果。

        Args:
            kwargs: 真实配置。

        Returns:
            受控模型。
        """
        return ChatDeepSeek(**kwargs, http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
    provider = DeepSeekTaskProvider(Settings(_env_file=None, deepseek_api_key="test-only"))
    with pytest.raises(ApplicationError) as failure:
        await provider.plan("规划任务")
    assert failure.value.code == code


async def test_task_provider_without_key_does_not_call_model() -> None:
    """缺少真实凭证时明确拒绝，不能返回伪造的默认计划。"""
    provider = DeepSeekTaskProvider(Settings(_env_file=None, deepseek_api_key=""))
    with pytest.raises(ApplicationError) as failure:
        await provider.plan("规划任务")
    assert failure.value.code == "MODEL_NOT_CONFIGURED"
