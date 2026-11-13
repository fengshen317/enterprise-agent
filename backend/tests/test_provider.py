"""使用真实 Deep Agents 图与模拟 HTTP 传输验证 SDK 适配。"""

import json

import httpx
import pytest

from app.agents.provider import DeepSeekProvider
from app.core.config import Settings
from app.core.errors import ApplicationError


def test_missing_model_key() -> None:
    """未配置密钥时不能静默使用测试回复。"""
    provider = DeepSeekProvider(Settings(_env_file=None, deepseek_api_key=""))
    with pytest.raises(ApplicationError, match="EA_DEEPSEEK_API_KEY"):
        provider.validate()


@pytest.mark.parametrize("finish_reason", ["stop", "length", "tool_calls"])
async def test_real_sdk_stream_with_mock_http(monkeypatch: pytest.MonkeyPatch, finish_reason: str) -> None:
    """运行真实图和供应商 SDK，确认流式正文及工具禁用行为。

    Args:
        monkeypatch: 替换网络传输而非替换 Agent 图。
        finish_reason: 供应商报告的结束原因。
    """
    from langchain_deepseek import ChatDeepSeek

    captured: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """返回供应商格式的 SSE，不访问网络。

        Args:
            request: SDK 发送的请求。

        Returns:
            模拟流式 HTTP 响应。
        """
        body = json.loads(request.content)
        captured.append(body)
        assert request.url.host == "api.deepseek.com"
        assert not body.get("tools")
        assert body["stream"] is True
        assert body["thinking"] == {"type": "disabled"}
        chunks = []
        for content, finish in [("你好", None), ("，世界", None), ("", finish_reason)]:
            chunk = {
                "id": "test-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": finish}],
            }
            if finish_reason == "tool_calls":
                chunk["choices"][0]["delta"] = (
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "task", "arguments": "{}"},
                            }
                        ]
                    }
                    if not chunks
                    else {}
                )
            chunks.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
        return httpx.Response(
            200, text="".join(chunks) + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:

        def model(**kwargs: object) -> ChatDeepSeek:
            """给真实 SDK 注入测试传输。

            Args:
                kwargs: 适配层的模型配置。

            Returns:
                带模拟网络的模型。
            """
            return ChatDeepSeek(**kwargs, http_async_client=client)

        monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
        provider = DeepSeekProvider(Settings(_env_file=None, deepseek_api_key="test-only"))
        provider.validate()
        if finish_reason == "tool_calls":
            with pytest.raises(ApplicationError, match="不支持执行工具"):
                _ = [part async for part in provider.stream([("user", "你好")])]
            return
        if finish_reason == "length":
            with pytest.raises(ApplicationError, match="模型回答达到长度限制"):
                _ = [part async for part in provider.stream([("user", "你好")])]
            return
        output = "".join([part async for part in provider.stream([("user", "你好")])])
    assert output == "你好，世界"
    assert len(captured) == 1
    assert captured[0]["messages"][-1] == {"role": "user", "content": "你好"}


@pytest.mark.parametrize("finish_reason", ["stop", "length", "tool_calls"])
async def test_summary_sdk_uses_nonstreaming_call(monkeypatch: pytest.MonkeyPatch, finish_reason: str) -> None:
    """摘要使用独立非流式调用，拒绝截断或工具返回。

    Args:
        monkeypatch: 替换网络传输。
        finish_reason: 供应商返回的结束原因。
    """
    from langchain_deepseek import ChatDeepSeek

    captured: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        """生成摘要完成响应。

        Args:
            request: SDK 请求。

        Returns:
            无网络的供应商响应。
        """
        body = json.loads(request.content)
        captured.append(body)
        assert not body.get("stream") and not body.get("tools")
        assert body["thinking"] == {"type": "disabled"}
        message = {"role": "assistant", "content": "保留项目代号与用户约定。"}
        if finish_reason == "tool_calls":
            message["tool_calls"] = [
                {"id": "call-1", "type": "function", "function": {"name": "task", "arguments": "{}"}}
            ]
        return httpx.Response(
            200,
            json={
                "id": "summary",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:

        def model(**kwargs: object) -> ChatDeepSeek:
            """注入模拟传输。

            Args:
                kwargs: 模型初始化参数。

            Returns:
                真实供应商适配器。
            """
            return ChatDeepSeek(**kwargs, http_async_client=client)

        monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
        provider = DeepSeekProvider(Settings(_env_file=None, deepseek_api_key="test-only"))
        if finish_reason != "stop":
            with pytest.raises(ApplicationError):
                await provider.summarize("旧摘要", [("user", "项目代号")], 200)
        else:
            assert await provider.summarize("旧摘要", [("user", "项目代号")], 200) == "保留项目代号与用户约定。"
    assert len(captured) == 1
    assert "200" in captured[0]["messages"][0]["content"]
    assert "旧摘要" in captured[0]["messages"][1]["content"]
    assert "项目代号" in captured[0]["messages"][2]["content"]
