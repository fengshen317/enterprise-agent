"""RSS 解析、时间窗口、故障隔离及实际请求目标的回归。"""

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.connectors.base import ConnectorError
from app.connectors.catalog import get_builtin
from app.connectors.rss import MAX_BYTES, FeedEntry, RssConnector, _public_ip, configured_feeds, fetch_feed, parse_feed


def test_rss_and_atom_preserve_sources_dates_and_safe_summaries() -> None:
    """RSS 的 GUID 链接及 Atom 更新日期都有明确来源，HTML 脚本被移除。"""
    rss = b"""<rss version="2.0"><channel><title>Official</title><item>
    <title>Release</title><guid>https://example.com/release</guid>
    <pubDate>Fri, 27 Nov 2026 08:00:00 +0800</pubDate>
    <description><![CDATA[<p>New model</p><script>secret()</script>]]></description>
    </item></channel></rss>"""
    entry = parse_feed(rss, "https://example.com/rss")[0]
    assert entry.url == "https://example.com/release" and entry.summary == "New model"
    assert entry.published_at == "2026-11-27T00:00:00+00:00" and entry.source == "Official"
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://example.com/blog/">
    <title>Labs</title><entry><id>urn:item:1</id><title>Update</title>
    <link rel="self" href="entry.xml"/><link href="update"/>
    <updated>2026-11-27T09:00:00+08:00</updated>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>One</p><p>Two</p></div></content>
    </entry></feed>"""
    entry = parse_feed(atom, "https://example.com/feed")[0]
    assert entry.url == "https://example.com/blog/update" and entry.summary == "One Two"
    assert entry.date_basis == "updated" and entry.published_at == "2026-11-27T01:00:00+00:00"


@pytest.mark.parametrize(
    "data",
    [
        b"<html><body>login</body></html>",
        b"<rss>",
        b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>',
        b"x" * (MAX_BYTES + 1),
    ],
)
def test_invalid_or_unsafe_feeds_are_rejected(data: bytes) -> None:
    """HTML、坏 XML、实体展开和超限响应均不会被当成空新闻。"""
    with pytest.raises(ConnectorError, match="RSS"):
        parse_feed(data, "https://example.com/feed")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/feed",
        "file:///etc/passwd",
        "https://user:secret@example.com/rss",
        "https://example.com:8443/rss",
        "",
    ],
)
def test_feed_config_refuses_credentials_and_non_https(url: str) -> None:
    """模型和配置均不能把连接器当作任意 URL 下载器。"""
    with pytest.raises(ConnectorError):
        configured_feeds({"feeds": json.dumps([url])})
    assert configured_feeds({"feeds": '["https://example.com/rss","https://example.com/rss"]'}) == [
        "https://example.com/rss"
    ]


async def test_dns_private_targets_are_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS 同时包含公网和私网地址时整体拒绝。"""
    lookup = AsyncMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(ConnectorError, match="公网"):
        await _public_ip("example.com")


async def test_download_pins_dns_and_rechecks_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求只连已验证的 IP，保留 TLS 域名，重定向到本机在第二跳被拦截。"""
    real_client = httpx.AsyncClient
    requested = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(302, headers={"location": "https://localhost/feed"})

    client = real_client(transport=httpx.MockTransport(serve))
    monkeypatch.setattr("app.connectors.rss.httpx.AsyncClient", lambda **kwargs: client)
    resolver = AsyncMock(side_effect=["8.8.8.8", ConnectorError("permission_denied", "private address")])
    monkeypatch.setattr("app.connectors.rss._public_ip", resolver)
    with pytest.raises(ConnectorError, match="private"):
        await fetch_feed("https://example.com/rss")
    assert len(requested) == 1 and requested[0].url.host == "8.8.8.8"
    assert requested[0].headers["host"] == "example.com"
    assert requested[0].extensions["sni_hostname"] == "example.com"
    assert [call.args[0] for call in resolver.await_args_list] == ["example.com", "localhost"]


async def test_recent_filters_dates_deduplicates_and_exposes_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一时窗反复读取可重试，源失败不隐藏，未知日期默认不当成最近新闻。"""
    stamp = datetime.now(UTC) - timedelta(hours=1)

    def item(identity: str, hours: int | None) -> FeedEntry:
        return FeedEntry(
            identity,
            "AI model",
            "https://example.com/" + identity,
            "summary",
            "Labs",
            "https://example.com/rss",
            (stamp - timedelta(hours=hours)).isoformat() if hours is not None else None,
            "pubDate",
        )

    rows = [item("one", 0), item("one", 0), item("two", 0), item("old", 48), item("future", -3), item("no-date", None)]

    async def load(url: str) -> list[FeedEntry]:
        if "broken" in url:
            raise ConnectorError("timeout", "timed out")
        return rows

    fetch = AsyncMock(side_effect=load)
    monkeypatch.setattr("app.connectors.rss.fetch_feed", fetch)
    connector = RssConnector({"feeds": '["https://example.com/rss","https://broken.example/rss"]'})
    assert get_builtin("rss") is not None
    assert (await connector.connect()).auth_status == "not_required"
    specs = connector.tool_specs(await connector.list_tools(), {})
    assert specs[0].key == "rss__recent" and specs[0].approval == "never"
    result = await connector.call("recent", {"hours": 24, "limit": 1})
    assert result["status"] == "partial" and result["truncated"] and result["matched_count"] == 2
    assert result["undated_count"] == 1 and len(result["items"]) == 1
    second = await connector.call("recent", {"hours": 24, "include_undated": True})
    assert second["matched_count"] == 3
    assert fetch.await_count == 3
    assert (await connector.check_health()).status == "degraded"
    empty = await connector.call("recent", {"query": "does-not-match"})
    assert empty["matched_count"] == 0
    await connector.aclose()


async def test_all_sources_failing_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """全失败会让调用失败，不能把抓取失败描述成没有新消息。"""
    monkeypatch.setattr("app.connectors.rss.fetch_feed", AsyncMock(side_effect=ConnectorError("timeout", "timeout")))
    connector = RssConnector({"feeds": '["https://example.com/rss"]'})
    with pytest.raises(ConnectorError, match="所有 RSS"):
        await connector.call("recent", {})


@pytest.mark.parametrize(
    "status,body",
    [
        (200, b"<rss><channel><title>Empty</title></channel></rss>"),
        (403, b"forbidden"),
        (200, b"x" * (MAX_BYTES + 1)),
    ],
)
async def test_fetch_empty_forbidden_and_oversize(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    """真正空源与 HTTP、响应大小失败具有不同结果。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body)))
    monkeypatch.setattr("app.connectors.rss.httpx.AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("app.connectors.rss._public_ip", AsyncMock(return_value="8.8.8.8"))
    if status == 200 and len(body) < MAX_BYTES:
        assert await fetch_feed("https://example.com/rss") == []
    else:
        with pytest.raises(ConnectorError):
            await fetch_feed("https://example.com/rss")


async def test_configuration_health_and_argument_errors() -> None:
    """安装后未配置会提示配置，坏参数无法进入网络层。"""
    for config in ({}, {"feeds": []}, {"feeds": ["https://example.com/"] * 11}, {"feeds": [""]}):
        connector = RssConnector(config)
        assert (await connector.connect()).status == "degraded"
        assert (await connector.check_health()).status == "degraded"
    connector = RssConnector({"feeds": ["https://example.com/rss"]})
    with pytest.raises(ConnectorError, match="参数"):
        await connector.call("recent", {"hours": 0})
    with pytest.raises(ConnectorError, match="未知"):
        await connector.call("unknown", {})


async def test_dns_lookup_failure_and_valid_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析异常分类为网络失败，有效公网地址原样交给连接层。"""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(side_effect=OSError("dns failed")))
    with pytest.raises(ConnectorError) as caught:
        await _public_ip("example.com")
    assert caught.value.code == "network_error"
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            ]
        ),
    )
    assert await _public_ip("example.com") == "8.8.8.8"


@pytest.mark.parametrize("failure", ["timeout", "missing_location", "redirect_loop"])
async def test_fetch_network_failures_are_bounded(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """超时和错误重定向明确失败，重定向循环最多发起四次请求。"""
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout")
        headers = {} if failure == "missing_location" else {"location": "/again"}
        return httpx.Response(302, headers=headers)

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    monkeypatch.setattr("app.connectors.rss.httpx.AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("app.connectors.rss._public_ip", AsyncMock(return_value="8.8.8.8"))
    with pytest.raises(ConnectorError) as caught:
        await fetch_feed("https://example.com/rss")
    assert caught.value.code == ("timeout" if failure == "timeout" else "invalid_output")
    assert len(requests) == (4 if failure == "redirect_loop" else 1)
