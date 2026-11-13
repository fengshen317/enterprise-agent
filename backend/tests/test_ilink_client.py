"""iLink 客户端按官方插件的请求形状组包。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.channels.ilink import ILinkClient, encode_client_version, quote_large_ids, safe_https_base
from app.channels.records import AccountRecord
from app.core.config import Settings


def _settings() -> Settings:
    """不读取开发环境文件。

    Returns:
        测试配置。
    """
    return Settings(
        _env_file=None,
        environment="test",
        task_scheduler_enabled=False,
        schedule_enabled=False,
        knowledge_enabled=False,
    )


def _account() -> AccountRecord:
    """构造一个已绑定账号。

    Returns:
        带明文 token 的账号。
    """
    return AccountRecord(
        id=uuid4(),
        user_id=uuid4(),
        workspace_id=uuid4(),
        channel_type="ilink",
        ilink_bot_id="bot-1",
        ilink_user_id="wx-user",
        baseurl="https://ilinkai.weixin.qq.com",
        updates_cursor="cursor-1",
        status="active",
        paused_until=None,
        created_at=datetime.now(UTC),
        token="bot-token",
    )


def test_channel_version_keeps_plugin_triplet_and_host_suffix() -> None:
    """宿主版本放在斜杠后，请求头只编码插件的前三段。"""
    assert encode_client_version("3.0.0/enterprise-agent-0.1.0") == "196608"
    assert encode_client_version("v3.0.0-beta-0") == "196608"


def test_quote_large_message_id_without_touching_strings() -> None:
    """超过 2^53 的 message_id 保持原值，正文里的同名文字不动。"""
    raw = '{"message_id":9007199254740993,"item_list":[{"text_item":{"text":"message_id: 1"}}]}'
    parsed = json.loads(quote_large_ids(raw))
    assert parsed["message_id"] == "9007199254740993"
    assert parsed["item_list"][0]["text_item"]["text"] == "message_id: 1"


def test_safe_base_rejects_private_and_non_https() -> None:
    """绑定响应里的内网或非 https 地址回退到默认主机。"""
    fallback = "https://ilinkai.weixin.qq.com"
    assert safe_https_base("http://127.0.0.1/ilink", fallback) == fallback
    assert safe_https_base("https://10.1.2.3", fallback) == fallback
    assert safe_https_base("https://ilinkai.weixin.qq.com/ilink/bot", fallback) == fallback


@pytest.mark.asyncio
async def test_qr_and_updates_match_protocol_headers() -> None:
    """取码、状态轮询和 getUpdates 的方法、头和 base_info 与协议一致。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/get_bot_qrcode"):
            return httpx.Response(200, json={"qrcode": "qr-1", "qrcode_img_content": "https://example.test/qr.png"})
        if request.url.path.endswith("/get_qrcode_status"):
            return httpx.Response(200, json={"status": "wait"})
        if request.url.path.endswith("/getupdates"):
            return httpx.Response(
                200,
                json={
                    "ret": -14,
                    "errcode": -14,
                    "errmsg": "session timeout",
                    "msgs": [{"message_id": 9007199254740993, "from_user_id": "wx-user"}],
                    "get_updates_buf": "next",
                },
            )
        return httpx.Response(404)

    settings = _settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ILinkClient(settings, http)
        qr = await client.get_bot_qrcode(settings.ilink_base_url)
        status = await client.get_qrcode_status("qr-1", settings.ilink_base_url)
        page = await client.get_updates(_account())

    assert qr.qrcode == "qr-1"
    assert status.status == "wait"
    assert page.session_expired is True
    assert page.messages == []

    qr_request, status_request, updates = seen
    assert qr_request.method == "POST"
    assert qr_request.url.params["bot_type"] == "3"
    assert json.loads(qr_request.content) == {"local_token_list": []}
    assert "Authorization" not in qr_request.headers
    assert qr_request.headers["AuthorizationType"] == "ilink_bot_token"
    assert qr_request.headers["iLink-App-Id"] == "bot"
    assert qr_request.headers["iLink-App-ClientVersion"] == "196608"
    assert "Authorization" not in status_request.headers
    assert "AuthorizationType" not in status_request.headers
    assert status_request.headers["iLink-App-Id"] == "bot"
    body = json.loads(updates.content)
    assert body["get_updates_buf"] == "cursor-1"
    assert body["base_info"]["channel_version"] == "3.0.0/enterprise-agent-0.1.0"
    assert body["base_info"]["bot_agent"] == "EnterpriseAgent/0.1.0"
    assert updates.headers["Authorization"] == "Bearer bot-token"


@pytest.mark.asyncio
async def test_send_message_echoes_context_token() -> None:
    """回复要带回入站 context_token，并标记为已完成的机器人文本。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ret": 0, "errmsg": ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ILinkClient(_settings(), http)
        await client.send_text(_account(), "wx-user", "ctx-token", "你好")

    message = json.loads(seen[0].content)["msg"]
    assert message["to_user_id"] == "wx-user"
    assert message["context_token"] == "ctx-token"
    assert message["from_user_id"] == ""
    assert message["message_type"] == 2
    assert message["message_state"] == 2
    assert message["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]


@pytest.mark.asyncio
async def test_long_poll_timeout_is_an_empty_page() -> None:
    """长轮询超时沿用官方客户端：返回空结果并保留游标。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    account = _account()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        page = await ILinkClient(_settings(), http).get_updates(account)
    assert page.session_expired is False
    assert page.messages == []
    assert page.cursor == "cursor-1"
