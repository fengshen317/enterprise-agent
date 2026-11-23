"""验证 QQ 邮箱适配器的只读协议、来源隔离与数据边界。"""

from __future__ import annotations

import base64
import imaplib
import ssl
from unittest.mock import MagicMock

import pytest

from app.connectors import qq_mail
from app.connectors.base import ConnectorError

_CREDS = {"email": "alice@example.com", "auth_code": "test-code"}
_HEADER = b"Subject: =?utf-8?b?5Lya6K6u?=\r\nFrom: alice@example.com\r\n\r\n"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """提供单次调用独立登录的 IMAP 协议替身。"""
    factory = MagicMock()
    client = factory.return_value.__enter__.return_value
    client.select.return_value = ("OK", [b"3"])
    client.response.return_value = ("UIDVALIDITY", [b"101"])
    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return factory


async def test_search_and_read(server: MagicMock) -> None:
    """确认 UTF-8 literal、日期边界、来源传递和附件元信息。"""
    client = server.return_value.__enter__.return_value
    header = ("OK", [(b"1 (UID 19 BODY[HEADER.FIELDS (SUBJECT FROM DATE)]<0> {63}", _HEADER), b")"])
    client.uid.side_effect = [("OK", [b"5 19 8"]), header]
    keyword = '预算"\r\n'
    result = await qq_mail.invoke(
        "search_messages",
        {
            "keyword": keyword,
            "since": "2026-11-01",
            "before": "2026-12-01",
            "limit": 1,
            "unread_only": True,
        },
        _CREDS,
    )
    assert isinstance(result, dict)
    source = result["messages"][0]["source_id"]
    assert result["messages"][0]["subject"] == "会议"
    assert result["matched_count"] == 3
    assert source.endswith(":INBOX:101:19")
    assert server.call_args.args == ("imap.qq.com", 993)
    assert server.call_args.kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    client.login.assert_called_once_with("alice@example.com", "test-code")
    client.select.assert_called_once_with("INBOX", readonly=True)
    assert client.literal == keyword.encode()
    assert client.uid.call_args_list[0].args == (
        "SEARCH",
        "CHARSET",
        "UTF-8",
        "UNSEEN",
        "SINCE",
        "01-Nov-2026",
        "BEFORE",
        "01-Dec-2026",
        "TEXT",
    )

    body = base64.b64encode("会议记录".encode())
    structure = (
        b'2 (UID 19 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "BASE64" 16 1)'
        b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 9000000 NIL ("ATTACHMENT" ("FILENAME" {10}'
    )
    client.uid.side_effect = [
        header,
        ("OK", [(structure, b"report.pdf"), b')) NIL NIL) "MIXED" NIL NIL NIL))']),
        ("OK", [(b"2 (UID 19 BODY[1]<0> {16}", body), b")"]),
    ]
    detail = await qq_mail.invoke("read_message", {"source_id": source}, _CREDS)
    assert isinstance(detail, dict)
    assert detail["source_id"] == source
    assert detail["body"] == "会议记录"
    assert not detail["truncated"]
    assert detail["attachments"] == [
        {"filename": "report.pdf", "content_type": "application/pdf", "encoded_bytes": 9000000}
    ]
    assert client.uid.call_args.args == ("FETCH", "19", "(BODY.PEEK[1]<0.32768>)")
    calls = [str(call.args) for call in client.uid.call_args_list]
    assert not any("BODY.PEEK[2]" in call or "RFC822" in call for call in calls)

    client.uid.reset_mock()
    for creds, ref in [({**_CREDS, "email": "bob@example.com"}, source), (_CREDS, source.replace(":101:", ":102:"))]:
        with pytest.raises(ConnectorError, match="当前邮箱或已失效"):
            await qq_mail.invoke("read_message", {"source_id": ref}, creds)
    client.uid.assert_not_called()


async def test_html_and_limit(server: MagicMock) -> None:
    """HTML 邮件只带出可见文本，保留截断标志。"""
    client = server.return_value.__enter__.return_value
    header = ("OK", [(b"1 (UID 1 BODY[HEADER.FIELDS (SUBJECT FROM DATE)]<0> {1}", _HEADER), b")"])
    client.uid.side_effect = [("OK", [b"1"]), header]
    result = await qq_mail.invoke("search_messages", {}, _CREDS)
    assert isinstance(result, dict)
    source = result["messages"][0]["source_id"]
    client.uid.side_effect = [
        header,
        ("OK", [b'1 (BODYSTRUCTURE ("TEXT" "HTML" NIL NIL NIL "8BIT" 99999 3) UID 1)']),
        ("OK", [(b"1 (BODY[1]<0> {64}", b"<style>hidden</style><script>bad()</script><p>hello</p>"), b")"]),
    ]
    detail = await qq_mail.invoke("read_message", {"source_id": source}, _CREDS)
    assert isinstance(detail, dict)
    assert detail["body"] == "hello"
    assert detail["truncated"]


async def test_bad_inputs_and_sanitized_login(server: MagicMock) -> None:
    """非法参数在连接前失败，服务端错误中的凭证不进入公开异常。"""
    invalid: tuple[tuple[str, dict[str, object]], ...] = (
        ("read_message", {"source_id": "19 OR ALL"}),
        ("search_messages", {"limit": 999}),
        ("search_messages", {"since": "2026-02-01", "before": "2026-01-01"}),
        ("search_messages", {"email": "other"}),
        ("delete", {}),
    )
    for name, arguments in invalid:
        with pytest.raises(ConnectorError):
            await qq_mail.invoke(name, arguments, _CREDS)
    with pytest.raises(ConnectorError):
        await qq_mail.invoke("search_messages", {}, {})
    server.assert_not_called()
    client = server.return_value.__enter__.return_value
    client.login.side_effect = imaplib.IMAP4.error("unsafe test-code")
    with pytest.raises(ConnectorError) as exc:
        await qq_mail.invoke("search_messages", {}, _CREDS)
    assert exc.value.code == "auth_failed"
    assert "test-code" not in str(exc.value)


def test_encoded_attachment_names() -> None:
    """中文文件名可来自 MIME 分段参数或旧式编码。"""
    assert (
        qq_mail._filename({"filename*0*": "utf-8''%E4%BC", "filename*1*": "%9A%E8%AE%AE.pdf"}, "filename") == "会议.pdf"
    )
    assert qq_mail._filename({"name": "=?utf-8?b?5Lya6K6u?="}, "name") == "会议"
