"""企业渠道：能力标记、官方请求形状、绑定码和微信窗口回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.channels.capabilities import (
    bind_instructions,
    capability_for,
    delivery_skip_reason,
    reminder_due,
    welcome_text,
)
from app.channels.commands import parse_inbound
from app.channels.dingtalk import DingTalkClient, classify_dingtalk_send, parse_dingtalk_message
from app.channels.feishu import FeishuClient, parse_feishu_card, parse_feishu_event
from app.channels.handler import InboundHandler
from app.channels.qq_bot import QQBotClient, api_base, parse_qq_dispatch
from app.channels.records import AccountRecord, AppRecord, PeerLink
from app.channels.send_error import ChannelSendError
from app.core.config import Settings
from app.services.push_channels import apply_wechat_flag, normalize_push_channels


def test_window_policy_follows_each_platform() -> None:
    """飞书不套用 24 小时文案；QQ 被动窗口是 60 分钟；钉钉主动推送要求先有单聊。"""
    settings = _settings()
    now = datetime.now(UTC)
    fresh = PeerLink(uuid4(), uuid4(), "peer", None, "ctx", now - timedelta(minutes=10))
    stale = PeerLink(uuid4(), uuid4(), "peer", None, "ctx", now - timedelta(hours=25))
    assert delivery_skip_reason(capability_for("ilink"), stale, now, settings) == "outside_window"
    assert delivery_skip_reason(capability_for("feishu"), None, now, settings) is None
    assert "超过 24 小时没有发消息" not in welcome_text("feishu")
    assert "没有 24 小时回复窗口" in welcome_text("feishu")
    assert "可用范围" in welcome_text("feishu")
    assert delivery_skip_reason(capability_for("dingtalk"), None, now, settings) == "no_prior_chat"
    assert delivery_skip_reason(capability_for("dingtalk"), fresh, now, settings) is None
    qq = capability_for("qq")
    assert qq.window_hours == 1
    assert qq.window_reply_budget == 4
    assert qq.text_progress is False
    assert delivery_skip_reason(qq, None, now, settings) is None
    assert "60 分钟" in welcome_text("qq")
    assert "4 次" in welcome_text("qq")
    assert reminder_due(capability_for("feishu"), fresh, now, settings) is None
    almost = PeerLink(uuid4(), uuid4(), "peer", None, "msg", now - timedelta(minutes=40))
    assert reminder_due(qq, almost, now, settings) == almost.last_inbound_at
    assert reminder_due(qq, fresh, now, settings) is None


def test_bind_command_and_push_channel_list() -> None:
    """绑定码是独立指令；渠道列表以勾选为准，微信开关只是其中一项。"""
    assert parse_inbound("/bind ab12cd34").kind == "bind"
    assert parse_inbound("/bind ab12cd34").argument == "ab12cd34"
    assert parse_inbound("同意").kind == "approve"
    assert normalize_push_channels(True, None) == (True, ["ilink"])
    assert normalize_push_channels(False, ["feishu", "qq", "feishu", "wecom"]) == (False, ["feishu", "qq"])
    assert apply_wechat_flag(["feishu"], True) == ["feishu", "ilink"]
    assert apply_wechat_flag(["feishu", "ilink"], False) == ["feishu"]
    assert "工作台账号" in bind_instructions("dingtalk")


def test_parsers_keep_groups_out_of_chat() -> None:
    """群消息被标成群，单聊才带着平台用户标识。"""
    feishu = parse_feishu_event(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"你好"}',
                },
            },
        }
    )
    assert feishu is not None and feishu.is_group is False and feishu.text == "你好"
    group = parse_feishu_event(
        {
            "event": {"message": {"message_id": "om_2", "chat_type": "group", "message_type": "text", "content": "{}"}},
        }
    )
    assert group is not None and group.is_group is True
    card = parse_feishu_card(
        {
            "header": {"event_id": "evt"},
            "event": {"operator": {"open_id": "ou_1"}, "action": {"value": {"decision": "approve", "code": "AB23"}}},
        }
    )
    assert card is not None and card.text == "同意 AB23"
    ding = parse_dingtalk_message(
        {
            "conversationType": "1",
            "senderStaffId": "user1",
            "msgId": "m",
            "msgtype": "text",
            "text": {"content": " 在吗 "},
        }
    )
    assert ding.is_group is False and ding.text == "在吗"
    grouped_ding = parse_dingtalk_message(
        {
            "conversationType": "2",
            "msgId": "g",
            "msgtype": "text",
            "text": {"content": "群"},
        }
    )
    assert grouped_ding.is_group
    qq = parse_qq_dispatch(
        {
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "mid", "content": "hi", "author": {"user_openid": "oid"}, "message_type": 0},
        }
    )
    assert qq is not None and qq.reply_message_id == "mid" and qq.is_group is False
    grouped = parse_qq_dispatch(
        {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "g1", "content": "@bot", "author": {"member_openid": "m"}},
        }
    )
    assert grouped is not None and grouped.is_group is True
    assert api_base(True).startswith("https://sandbox.")


@pytest.mark.asyncio
async def test_outbound_requests_match_official_shapes() -> None:
    """出站 URL、身份头和错误分类按各平台当前文档。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        if request.url.path.endswith("/messages") and "feishu" in str(request.url):
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "d-1", "expireIn": 7200})
        if request.url.path.endswith("/batchSend"):
            return httpx.Response(200, json={"flowControlledStaffIdList": ["user1"]})
        if request.url.path.endswith("/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "q-1", "expires_in": 7200})
        if "/v2/users/" in request.url.path:
            return httpx.Response(400, json={"message": "not friend"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        app = AppRecord(uuid4(), uuid4(), "feishu", "cli_1", "active", secret="sec")
        feishu = FeishuClient(http)
        await feishu.send_text(app, "ou_1", "你好")
        feishu_send = seen[-1]
        assert feishu_send.url.params["receive_id_type"] == "open_id"
        assert feishu_send.headers["authorization"] == "Bearer t-1"
        ding = DingTalkClient(http)
        ding_app = AppRecord(uuid4(), uuid4(), "dingtalk", "appkey", "active", robot_code="robot", secret="sec")
        with pytest.raises(ChannelSendError) as limited:
            await ding.send_text(ding_app, "user1", "你好")
        assert limited.value.reason == "flow_control" and limited.value.retryable is False
        qq = QQBotClient(http)
        qq_app = AppRecord(uuid4(), uuid4(), "qq", "123", "active", secret="sec")
        with pytest.raises(ChannelSendError) as rejected:
            await qq.send_text(qq_app, "openid", "你好", msg_id="mid", msg_seq=1)
        assert rejected.value.reason == "not_friend"
    assert classify_dingtalk_send(400, {"code": "robot.oto.notExist"}).reason == "no_prior_chat"
    assert classify_dingtalk_send(400, {"code": "chatbotId.notAllow.sendOTO"}).retryable is False


class _Repo:
    """只覆盖未绑定入站需要的存储方法。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []
        self.cleared = False

    async def record_app_event(
        self,
        app_id: object,
        message_id: str,
        peer_id: str,
        status: str,
        reason: str | None,
        excerpt: str,
    ) -> bool:
        del app_id, peer_id, excerpt
        self.events.append((message_id, reason if status == "skipped" else status))
        return message_id != "dup"

    async def redeem_bind_code(self, channel_type: str, peer_id: str, code: str) -> AccountRecord | None:
        del channel_type
        if code != "ABCD2345":
            return None
        return AccountRecord(
            uuid4(),
            uuid4(),
            uuid4(),
            "feishu",
            "cli",
            peer_id,
            "",
            "",
            "active",
            None,
            datetime.now(UTC),
            welcome_pending=True,
        )

    async def clear_welcome(self, account_id: object) -> None:
        del account_id
        self.cleared = True


class _Bus:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_app_text(self, app: AppRecord, peer_id: str, text: str) -> None:
        del app, peer_id
        self.sent.append(text)

    async def send_text(self, account: AccountRecord, to_user_id: str, context_token: str, text: str) -> None:
        del account, to_user_id, context_token
        self.sent.append(text)

    async def send_typing(self, account: AccountRecord, to_user_id: str, context_token: str, active: bool) -> None:
        del account, to_user_id, context_token, active


def _settings() -> Settings:
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="test",
        task_scheduler_enabled=False,
        schedule_enabled=False,
        knowledge_enabled=False,
        channel_token_key=SecretStr(Fernet.generate_key().decode()),
    )


@pytest.mark.asyncio
async def test_unbound_user_gets_instructions_and_bind_does_not_guess() -> None:
    """没有绑定码时不创建账号；正确的码才会绑定到发码的人。"""
    repo = _Repo()
    bus = _Bus()
    handler = InboundHandler(_settings(), repo, bus, object(), object(), object(), object())  # type: ignore[arg-type]
    app = AppRecord(uuid4(), uuid4(), "feishu", "cli", "active", secret="sec")
    from app.channels.inbound import InboundEvent

    await handler.handle_unbound(app, InboundEvent("g1", "ou", "群里说话", True))
    assert repo.events == [("g1", "group")]
    assert bus.sent == []
    await handler.handle_unbound(app, InboundEvent("m1", "ou", "你好", False))
    assert "绑定码" in bus.sent[-1]
    assert not any(item[0] == "m1" and item[1] not in {"received", None} for item in repo.events)
    await handler.handle_unbound(app, InboundEvent("m2", "ou", "/bind NOPE1234", False))
    assert "不正确或已过期" in bus.sent[-1]
    await handler.handle_unbound(app, InboundEvent("m3", "ou", "/bind ABCD2345", False))
    assert bus.sent[-1].startswith("已绑定飞书")
    assert "超过 24 小时没有发消息" not in bus.sent[-1]
    assert repo.cleared is True
