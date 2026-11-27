"""公开 RSS 2.0 / Atom 订阅，按调用时间窗口提供带来源的简报材料。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorKind,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
    health_exception,
    make_tool_key,
)

MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 200
FEED_TIMEOUT = 8
ATOM = "{http://www.w3.org/2005/Atom}"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"


def feed_url(value: str) -> httpx.URL:
    """只允许无需认证的标准 HTTPS 订阅地址，防止配置携带凭据。"""
    try:
        url = httpx.URL(value)
        if url.scheme != "https" or not url.host or url.port not in (None, 443) or url.userinfo:
            raise ValueError
        return url.copy_with(fragment=None)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ConnectorError("not_configured", "RSS 地址必须是无需认证的 HTTPS 地址，端口为 443") from exc


def configured_feeds(config: Mapping[str, object]) -> list[str]:
    """解析插件配置页保存的 JSON 数组。

    Args:
        config: 来自数据库的共享配置，feeds 可以是 JSON 字符串或字符串数组。

    Returns:
        去重后的 HTTPS 订阅地址。

    Raises:
        ConnectorError: 地址、数量或 JSON 格式无效。
    """
    raw = config.get("feeds", "[]")
    try:
        loaded = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ConnectorError("not_configured", "订阅地址应填写 JSON 字符串数组") from exc
    if not isinstance(loaded, list) or not 1 <= len(loaded) <= 10:
        raise ConnectorError("not_configured", "请配置 1 到 10 个 RSS / Atom 订阅地址")
    urls: list[str] = []
    for value in loaded:
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ConnectorError("not_configured", "每个订阅必须是有效地址，最长 2000 字符")
        url = str(feed_url(value.strip()))
        if url not in urls:
            urls.append(url)
    return urls


class RecentArgs(BaseModel):
    """只查询管理员配置的源，模型不能临时指定网络目标。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    hours: int = Field(default=24, ge=1, le=168, description="最近多少小时，以文章发布时间计算，最大 7 天")
    limit: int = Field(default=20, ge=1, le=50)
    query: str = Field(default="", max_length=100, description="可选，标题和摘要中匹配的关键词")
    include_undated: bool = Field(default=False, description="是否附带无法确认发布时间的条目，默认排除")


class _PlainText(HTMLParser):
    """提取摘要中的文本，脚本和样式内容不进入模型上下文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(value: str, limit: int) -> str:
    """把嵌入 HTML 收成有界纯文本。"""
    parser = _PlainText()
    parser.feed(value[:10000])
    return " ".join(" ".join(parser.parts).split())[:limit]


def _value(node: Element, path: str) -> str:
    """读出文本构造，兼容 Atom 的 XHTML 子节点。"""
    child = node.find(path)
    return "" if child is None else " ".join(child.itertext()).strip()


def _timestamp(value: str) -> datetime | None:
    """支持 RFC 822 和带时区 ISO 时间，无时区或无效时间不猜测。"""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return stamp.astimezone(UTC) if stamp.tzinfo is not None else None


@dataclass(frozen=True)
class FeedEntry:
    """源中的一条文章，缺失时间和链接均保留为空。"""

    id: str
    title: str
    url: str
    summary: str
    source: str
    feed_url: str
    published_at: str | None
    date_basis: str | None


def parse_feed(data: bytes, base_url: str) -> list[FeedEntry]:
    """安全解析 RSS 2.0 或 Atom。

    Args:
        data: 有界 XML 响应体。
        base_url: 重定向后的源地址，用于解析相对链接。

    Returns:
        最多 200 条文章，保留日期依据及原文链接。

    Raises:
        ConnectorError: 响应超限、包含禁止的 XML 构造或格式不支持。
    """
    if len(data) > MAX_BYTES:
        raise ConnectorError("invalid_output", "RSS 响应超过 2 MiB")
    try:
        root: Element = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError, LookupError) as exc:
        raise ConnectorError("invalid_output", "返回内容不是可安全解析的 RSS / Atom") from exc
    atom = root.tag == ATOM + "feed"
    if atom:
        container = root
        nodes = root.findall(ATOM + "entry")
        prefix = ATOM
    else:
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ConnectorError("invalid_output", "仅支持 RSS 2.0 和 Atom 1.0")
        container, nodes, prefix = channel, channel.findall("item"), ""
    source = _text(_value(container, prefix + "title"), 200) or httpx.URL(base_url).host
    base = urljoin(base_url, root.get(XML_BASE, ""))
    result: list[FeedEntry] = []
    for node in nodes[:MAX_ENTRIES]:
        title = _text(_value(node, prefix + "title"), 300)
        link = _value(node, "link")
        if not atom and not link:
            guid = node.find("guid")
            if guid is not None and guid.get("isPermaLink", "true").lower() == "true":
                candidate = (guid.text or "").strip()
                link = candidate if candidate.startswith(("http://", "https://")) else ""
        if atom:
            link = next(
                (
                    item.get("href", "")
                    for item in node.findall(ATOM + "link")
                    if item.get("rel", "alternate") == "alternate"
                ),
                "",
            )
        url = urljoin(urljoin(base, node.get(XML_BASE, "")), link) if link else ""
        try:
            parsed_url = httpx.URL(url)
            url = (
                str(parsed_url.copy_with(fragment=None))
                if (parsed_url.scheme in {"http", "https"} and parsed_url.host and not parsed_url.userinfo)
                else ""
            )
        except httpx.InvalidURL:
            url = ""
        basis = "published" if atom else "pubDate"
        stamp = _timestamp(_value(node, prefix + basis))
        if atom and stamp is None:
            basis, stamp = "updated", _timestamp(_value(node, ATOM + "updated"))
        summary = _value(node, prefix + ("summary" if atom else "description"))
        if not summary:
            summary = _value(node, ATOM + "content" if atom else "{http://purl.org/rss/1.0/modules/content/}encoded")
        identity = _value(node, prefix + ("id" if atom else "guid"))[:1000]
        if not title and not summary:
            continue
        result.append(
            FeedEntry(
                identity or url or title,
                title,
                url,
                _text(summary, 1500),
                source,
                base_url,
                stamp.isoformat() if stamp else None,
                basis if stamp else None,
            )
        )
    return result


async def _public_ip(host: str) -> str:
    """验证所有 DNS 结果，并返回实际连接的公网 IP，避免二次解析改变目标。"""
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ConnectorError("network_error", "订阅源域名解析失败") from exc
    ips = [str(item[4][0]) for item in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ConnectorError("permission_denied", "RSS 订阅仅允许访问公网地址")
    return ips[0]


async def fetch_feed(url: str) -> list[FeedEntry]:
    """读取订阅，每跳都连接已校验的公网 IP。

    Args:
        url: 管理员配置的公开 HTTPS 订阅地址。

    Returns:
        安全解析后的条目。

    Raises:
        ConnectorError: 请求超时、网络失败、目标不公开或内容格式异常。
    """
    current = feed_url(url)
    try:
        async with (
            asyncio.timeout(FEED_TIMEOUT),
            httpx.AsyncClient(
                timeout=FEED_TIMEOUT,
                trust_env=False,
                follow_redirects=False,
                # 不同域名可能共享一个 IP，每跳重新握手以校验对应域名的证书。
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client,
        ):
            for _ in range(4):
                ip = await _public_ip(current.host)
                target = current.copy_with(host=ip)
                # TLS 仍校验原域名证书，HTTP Host 保留订阅域名。
                async with client.stream(
                    "GET",
                    target,
                    headers={"Host": current.host, "User-Agent": "EnterpriseAgent-RSS/1.0"},
                    extensions={"sni_hostname": current.host},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise ConnectorError("invalid_output", "订阅源重定向缺少地址")
                        current = feed_url(urljoin(str(current), location))
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_BYTES:
                            raise ConnectorError("invalid_output", "RSS 响应超过 2 MiB")
                    return parse_feed(bytes(content), str(current))
        raise ConnectorError("invalid_output", "RSS 订阅重定向过多")
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ConnectorError("timeout", "订阅源响应超时") from exc
    except httpx.HTTPError as exc:
        raise ConnectorError("network_error", "订阅源请求失败，请检查地址与网络") from exc


class RssConnector:
    """按需读取公开订阅源，工具可用于普通问答和现有定时任务。"""

    key = "rss"
    kind: ConnectorKind = "api"

    def __init__(self, config: dict[str, object]) -> None:
        """保存共享订阅配置，缓存只保留本实例最近五分钟的解析结果。"""
        self._config = config
        self._cache: dict[str, tuple[float, list[FeedEntry]]] = {}
        self._lock = asyncio.Lock()

    def config_fields(self) -> tuple[ConfigField, ...]:
        """复用插件配置表单，JSON 数组支持多个订阅地址。"""
        return (
            ConfigField(
                "feeds",
                "公开 RSS / Atom 订阅地址（JSON 数组，最多 10 个）",
                "json",
                required=True,
                default="[]",
                placeholder='["https://openai.com/news/rss.xml"]',
            ),
        )

    async def connect(self) -> ConnectorStatusInfo:
        """本地校验配置，真实网络检查通过手动测试执行。"""
        try:
            configured_feeds(self._config)
        except ConnectorError as exc:
            return health_exception(exc)
        return ConnectorStatusInfo(status="connected", auth_status="not_required")

    async def check_health(self) -> ConnectorStatusInfo:
        """探测全部已配置源，任一不可用都在插件页显示待检查状态。"""
        try:
            result = await self.call("recent", {"hours": 24, "limit": 1})
            if result["status"] == "partial":
                return ConnectorStatusInfo(
                    status="degraded",
                    auth_status="not_required",
                    error_code="network_error",
                    message="部分 RSS 源不可用，查询结果中有逐源状态",
                )
        except ConnectorError as exc:
            return health_exception(exc)
        return ConnectorStatusInfo(status="connected", auth_status="not_required")

    async def list_tools(self) -> list[ConnectorToolDef]:
        """提供强类型的最近文章查询工具。"""
        return [
            ConnectorToolDef(
                "recent",
                "查询已配置 RSS / Atom 源最近的文章，返回摘要与原文链接。"
                "结果仅覆盖订阅当前保留条目，需注明失败源、无日期条目和结果截断。"
                "每次按时间范围查询，不记录已投递状态。",
                RecentArgs.model_json_schema(),
                risk="read",
                approval="never",
            )
        ]

    def tool_specs(self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]) -> list[ToolSpec]:
        """接入现有权限、配额和审计，不把文章正文写入参数审计。"""
        definition = defs[0]
        override = policy.get("recent")

        async def handler(context: ToolContext, arguments: BaseModel) -> object:
            return await self.call("recent", arguments.model_dump())

        def audit(arguments: BaseModel, result: object) -> dict[str, object]:
            data = result if isinstance(result, dict) else {}
            return {
                "hours": arguments.model_dump().get("hours"),
                "status": data.get("status"),
                "returned_count": data.get("returned_count"),
            }

        return [
            ToolSpec(
                key=make_tool_key(self.key, "recent"),
                version=1,
                display_name="RSS 最近文章",
                description=definition.description,
                args_model=RecentArgs,
                risk=override.risk if override else "read",
                approval=override.approval if override else "never",
                timeout_seconds=30,
                quota_per_run=2,
                handler=handler,
                audit=audit,
                source="connector",
                connector_key=self.key,
            )
        ]

    async def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """读取订阅并按日期过滤，局部失败保留其他源结果。

        Args:
            name: 固定工具名 recent。
            arguments: 时间范围、数量、关键词及未知日期开关。

        Returns:
            文章、逐源状态、查询窗口和截断信息。

        Raises:
            ConnectorError: 参数错误、配置无效或所有源读取失败。
        """
        if name != "recent":
            raise ConnectorError("unknown_tool", "未知 RSS 工具")
        try:
            args = RecentArgs.model_validate(arguments)
        except ValidationError as exc:
            raise ConnectorError("invalid_arguments", "RSS 查询参数不正确") from exc
        urls = configured_feeds(self._config)
        semaphore = asyncio.Semaphore(5)

        async def load(url: str) -> tuple[str, list[FeedEntry], str | None]:
            """有界并发抓取单个源，失败不写入缓存。"""
            async with semaphore:
                cached = self._cache.get(url)
                if cached is not None and monotonic() - cached[0] < 300:
                    return url, cached[1], None
                try:
                    entries = await fetch_feed(url)
                except ConnectorError as exc:
                    return url, [], exc.code
                self._cache[url] = (monotonic(), entries)
                return url, entries, None

        async with self._lock:
            feeds = await asyncio.gather(*(load(url) for url in urls))
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=args.hours)
        items: dict[str, FeedEntry] = {}
        undated = 0
        reports: list[dict[str, object]] = []
        for url, entries, error in feeds:
            reports.append(
                {
                    "url": url,
                    "status": "failed" if error else "ok",
                    "error_code": error,
                    "parsed_count": len(entries),
                    "entry_limit_reached": len(entries) >= MAX_ENTRIES,
                }
            )
            for entry in entries:
                stamp = datetime.fromisoformat(entry.published_at) if entry.published_at else None
                if stamp is None:
                    undated += 1
                    if not args.include_undated:
                        continue
                elif not cutoff <= stamp <= now:
                    continue
                if args.query.strip().casefold() not in (entry.title + " " + entry.summary).casefold():
                    continue
                # 有原文链接时跨源去重；缺链接的条目只在同一订阅内按 GUID 去重。
                key = entry.url or entry.feed_url + "#" + entry.id
                items.setdefault(key, entry)
        failures = sum(error is not None for _, _, error in feeds)
        if failures == len(feeds):
            errors = {error for _, _, error in feeds if error is not None}
            code = errors.pop() if len(errors) == 1 else "network_error"
            raise ConnectorError(code, "所有 RSS 源均读取失败，请在插件配置页检查订阅地址和网络解析")
        ordered = sorted(items.values(), key=lambda entry: entry.published_at or "", reverse=True)
        return {
            "status": "partial" if failures else "available",
            "feeds": reports,
            "window_start": cutoff.isoformat(),
            "window_end": now.isoformat(),
            "undated_count": undated,
            "matched_count": len(ordered),
            "returned_count": min(len(ordered), args.limit),
            "truncated": len(ordered) > args.limit,
            "items": [asdict(entry) for entry in ordered[: args.limit]],
        }

    async def aclose(self) -> None:
        """停用或修改配置时清除实例缓存。"""
        self._cache.clear()
