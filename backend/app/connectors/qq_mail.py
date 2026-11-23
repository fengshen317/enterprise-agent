"""使用 QQ 邮箱 IMAP 服务读取当前绑定账号的收件箱。"""

from __future__ import annotations

import asyncio
import hashlib
import imaplib
import re
import ssl
from dataclasses import dataclass
from datetime import date
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.connectors.base import ConnectorError, ConnectorToolDef

_HEADER_LIMIT = 8192
_BODY_LIMIT = 32768
_EXCERPT_LIMIT = 6000
_SEARCH_LIMIT = 20
_TIMEOUT = 12
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_UID = re.compile(rb"[1-9][0-9]{0,9}")
_SOURCE = r"^qq_mail:[0-9a-f]{24}:INBOX:[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$"


class SearchArgs(BaseModel):
    """按收件日期、关键词或未读状态检索。"""

    model_config = ConfigDict(extra="forbid")
    unread_only: bool = False
    since: date | None = Field(None, description="收件日期起点，含当天")
    before: date | None = Field(None, description="收件日期终点，不含当天")
    keyword: str = Field("", max_length=160, description="在邮件头及正文中检索的文字")
    limit: int = Field(10, ge=1, le=_SEARCH_LIMIT)

    @model_validator(mode="after")
    def validate_range(self) -> SearchArgs:
        """确保起止日期组成有效的半开区间。"""
        if self.since and self.before and self.since >= self.before:
            raise ValueError("结束日期须晚于开始日期")
        return self


class ReadArgs(BaseModel):
    """沿用检索结果的来源标识读取单封邮件。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(pattern=_SOURCE)


TOOLS: tuple[ConnectorToolDef, ...] = (
    ConnectorToolDef(
        "search_messages",
        "搜索本人 QQ 收件箱，按最新 UID 返回邮件摘要和来源 ID。",
        SearchArgs.model_json_schema(),
        "read",
        "never",
    ),
    ConnectorToolDef(
        "read_message", "按来源 ID 阅读邮件的纯文本摘录与附件元信息。", ReadArgs.model_json_schema(), "read", "never"
    ),
)


class _VisibleText(HTMLParser):
    """仅提取可见文本，忽略样式、脚本和外部资源。"""

    def __init__(self) -> None:
        """初始化可见文本片段。"""
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """跳过非正文区域，保留段落边界。"""
        if tag in {"style", "script", "head", "template"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """在非正文区域结束后恢复提取。"""
        if tag in {"style", "script", "head", "template"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        """记录可见文本。"""
        if not self.hidden:
            self.parts.append(data)


def _error() -> ConnectorError:
    """返回不包含私人邮件内容的协议错误。"""
    return ConnectorError("invalid_output", "邮箱返回了无法解析的邮件数据")


def _fetch(client: imaplib.IMAP4_SSL, uid: str, fields: str) -> list[object]:
    """按 UID 读取指定字段，并处理查询期间邮件已被移除的情况。"""
    status, rows = client.uid("FETCH", uid, f"({fields})")
    if status != "OK":
        raise ConnectorError("command_failed", "邮箱拒绝读取邮件")
    if not rows or rows == [None]:
        raise ConnectorError("not_found", "邮件已移除，请重新搜索")
    return list(rows)


def _partial(client: imaplib.IMAP4_SSL, uid: str, section: str, limit: int) -> bytes:
    """使用 PEEK 及服务端字节上限，保留原有已读状态。"""
    for row in _fetch(client, uid, f"BODY.PEEK[{section}]<0.{limit}>"):
        if isinstance(row, tuple) and len(row) == 2 and isinstance(row[1], bytes):
            return row[1][:limit]
    raise _error()


def _summary(client: imaplib.IMAP4_SSL, prefix: str, uid: str) -> dict[str, object]:
    """只请求展示所需的少数邮件头字段。"""
    raw = _partial(client, uid, "HEADER.FIELDS (SUBJECT FROM DATE)", _HEADER_LIMIT)
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    return {
        "source_id": f"{prefix}:{uid}",
        "subject": str(message.get("Subject", "")),
        "sender": str(message.get("From", "")),
        "date": str(message.get("Date", "")),
    }


def _search(client: imaplib.IMAP4_SSL, prefix: str, args: SearchArgs) -> dict[str, object]:
    """检索符合全部条件的邮件，优先返回较新的 UID。"""
    terms = ["UNSEEN" if args.unread_only else "ALL"]
    for key, day in (("SINCE", args.since), ("BEFORE", args.before)):
        if day:
            terms.extend((key, f"{day.day:02}-{_MONTHS[day.month - 1]}-{day.year:04}"))
    if args.keyword:
        terms = ["CHARSET", "UTF-8", *terms, "TEXT"]
        # IMAP literal 单独按长度传输，中文与引号均不会变成协议指令。
        client.literal = args.keyword.encode("utf-8")  # type: ignore[assignment]  # imaplib 将 literal 原样写入套接字。
    status, rows = client.uid("SEARCH", *terms)
    if status != "OK":
        raise ConnectorError("command_failed", "邮箱拒绝当前搜索条件")
    if not rows or not isinstance(rows[0], bytes):
        raise _error()
    ids = rows[0].split()
    if any(not _UID.fullmatch(uid) for uid in ids):
        raise _error()
    messages = []
    for uid in sorted(ids, key=int, reverse=True)[: args.limit]:
        try:
            messages.append(_summary(client, prefix, uid.decode("ascii")))
        except ConnectorError as exc:
            if exc.code != "not_found":
                raise
    return {"messages": messages, "matched_count": len(ids)}


type _Node = str | list[_Node]
_TOKENS = re.compile(rb'\s+|[()]|"(?:\\.|[^"\\])*"|[^\s()"]+')


def _structure(rows: list[object]) -> list[_Node]:
    """解析 IMAP 嵌套列表，并将 imaplib 分离的 literal 还原为字符串。"""
    wire = bytearray()
    for row in rows:
        if isinstance(row, bytes):
            wire.extend(row)
        elif isinstance(row, tuple) and len(row) == 2 and all(isinstance(v, bytes) for v in row):
            head, value = row
            wire.extend(re.sub(rb"\{\d+\}$", b"", head))
            wire.extend(b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"')
        else:
            raise _error()
    stack: list[list[_Node]] = [[]]
    for token in _TOKENS.findall(bytes(wire)):
        if token == b"(":
            child: list[_Node] = []
            stack[-1].append(child)
            stack.append(child)
            if len(stack) > 50:
                raise _error()
        elif token == b")":
            if len(stack) == 1:
                raise _error()
            stack.pop()
        elif not token.isspace():
            if token.startswith(b'"'):
                token = re.sub(rb"\\(.)", rb"\1", token[1:-1])
            stack[-1].append(token.decode("utf-8", "replace"))
    if len(stack) == 1:
        for row_node in stack[0]:
            if isinstance(row_node, list):
                for index, field in enumerate(row_node[:-1]):
                    value_node = row_node[index + 1]
                    if isinstance(field, str) and field.upper() == "BODYSTRUCTURE" and isinstance(value_node, list):
                        return value_node
    raise _error()


@dataclass
class _Part:
    """正文候选分段的取数及解码信息。"""

    section: str
    mime: str
    charset: str
    encoding: str
    size: int


def _parameters(node: _Node) -> dict[str, str]:
    """按 MIME 参数名折叠大小写。"""
    if not isinstance(node, list):
        return {}
    return {
        key.lower(): val
        for key, val in zip(node[::2], node[1::2], strict=False)
        if isinstance(key, str) and isinstance(val, str)
    }


def _filename(parameters: dict[str, str], name: str) -> str:
    """使用标准 MIME 解码展开中文文件名及 RFC 2231 分段参数。"""
    fields = ["attachment"]
    for key, value in parameters.items():
        if re.fullmatch(rf"{name}(?:\*(?:[0-9]+\*?)?)?", key):
            escaped = value.replace("\r", "").replace("\n", "").replace("\\", "\\\\").replace('"', '\\"')
            fields.append(f'filename{key[len(name) :]}="{escaped}"')
    message = EmailMessage(policy=policy.default)
    message["Content-Disposition"] = "; ".join(fields)
    return message.get_filename() or ""


def _collect(node: list[_Node], section: str, bodies: list[_Part], attachments: list[dict[str, object]]) -> None:
    """遍历 MIME 树，禁止把附件或附带转发邮件当作当前邮件的正文。"""
    if not node:
        raise _error()
    if isinstance(node[0], list):
        children = []
        for child in node:
            if not isinstance(child, list):
                break
            children.append(child)
        subtype = str(node[len(children)]).lower() if len(node) > len(children) else "mixed"
        mime = f"multipart/{subtype}"
        disposition_at = len(children) + 2
        size = None
        params = {}
    else:
        if len(node) < 7 or not str(node[6]).isdigit():
            raise _error()
        children = []
        mime = f"{node[0]}/{node[1]}".lower()
        size = int(str(node[6]))
        params = _parameters(node[2])
        disposition_at = 11 if mime == "message/rfc822" else 9 if mime.startswith("text/") else 8
    disposition = node[disposition_at] if len(node) > disposition_at else None
    disp = ""
    filename = _filename(params, "name")
    if isinstance(disposition, list) and len(disposition) >= 2:
        disp = str(disposition[0]).lower()
        filename = _filename(_parameters(disposition[1]), "filename") or filename
    if filename or disp == "attachment" or (not children and not mime.startswith("text/")):
        attachments.append({"filename": filename, "content_type": mime, "encoded_bytes": size})
    elif children:
        for index, child in enumerate(children, 1):
            _collect(child, f"{section}.{index}" if section else str(index), bodies, attachments)
    elif size is not None and mime in {"text/plain", "text/html"}:
        bodies.append(_Part(section or "1", mime, params.get("charset", "utf-8"), str(node[5]), size))


def _read(client: imaplib.IMAP4_SSL, prefix: str, args: ReadArgs) -> dict[str, object]:
    """校验来源后仅读取邮件头、附件目录以及一个正文分段的开头。"""
    parent, uid = args.source_id.rsplit(":", 1)
    if parent != prefix:
        raise ConnectorError("not_found", "来源不属于当前邮箱或已失效，请重新搜索")
    result = _summary(client, prefix, uid)
    bodies: list[_Part] = []
    attachments: list[dict[str, object]] = []
    _collect(_structure(_fetch(client, uid, "BODYSTRUCTURE")), "", bodies, attachments)
    result.update(body="", truncated=False, attachments=attachments)
    if bodies:
        part = next((part for part in bodies if part.mime == "text/plain"), bodies[0])
        raw = _partial(client, uid, part.section, _BODY_LIMIT)
        message = EmailMessage(policy=policy.default)
        message["Content-Transfer-Encoding"] = part.encoding
        payload = raw
        if part.size > len(raw) and part.encoding.lower() == "base64":
            # 服务端按传输字节截断，末尾可能处于 base64 四字符组中间。
            compact = b"".join(raw.split())
            payload = compact[: len(compact) - len(compact) % 4]
        message.set_payload(payload)
        decoded = message.get_payload(decode=True)
        if not isinstance(decoded, bytes):
            raise _error()
        try:
            body = decoded.decode(part.charset, "replace")
        except (LookupError, UnicodeError):
            body = decoded.decode("utf-8", "replace")
        if part.mime == "text/html":
            parser = _VisibleText()
            parser.feed(body)
            parser.close()
            body = "".join(parser.parts)
        body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", body).strip()
        result.update(body=body[:_EXCERPT_LIMIT], truncated=part.size > len(raw) or len(body) > _EXCERPT_LIMIT)
    return result


def _run(args: SearchArgs | ReadArgs, email: str, auth_code: str) -> dict[str, object]:
    """用本次调用绑定的凭证建立独立 TLS 会话。"""
    try:
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL("imap.qq.com", 993, ssl_context=context, timeout=_TIMEOUT) as client:
            try:
                client.login(email, auth_code)
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error:
                raise ConnectorError("auth_failed", "QQ 邮箱授权失败，请检查 IMAP 开关和授权码") from None
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise ConnectorError("permission_denied", "无法只读访问收件箱")
            _, validity = client.response("UIDVALIDITY")
            if not validity or not isinstance(validity[0], bytes) or not _UID.fullmatch(validity[0]):
                raise _error()
            account = hashlib.sha256(email.casefold().encode()).hexdigest()[:24]
            prefix = f"qq_mail:{account}:INBOX:{validity[0].decode('ascii')}"
            if isinstance(args, SearchArgs):
                return _search(client, prefix, args)
            return _read(client, prefix, args)
    except TimeoutError:
        raise ConnectorError("timeout", "连接 QQ 邮箱超时") from None
    except (OSError, imaplib.IMAP4.abort):
        raise ConnectorError("network_error", "QQ 邮箱连接中断") from None
    except imaplib.IMAP4.error:
        raise ConnectorError("command_failed", "邮箱未能完成只读请求") from None
    except (ValueError, UnicodeError):
        raise _error() from None


async def invoke(name: str, arguments: dict[str, object], credentials: dict[str, str]) -> object:
    """执行当前用户授权的 QQ 邮箱只读操作。

    Args:
        name: 工具发现返回的原始工具名。
        arguments: 邮件检索条件或来源标识。
        credentials: 当前个人绑定中的 email 与 auth_code。

    Returns:
        含来源标识的搜索结果或单封邮件详情。
    """
    email = credentials.get("email", "").strip()
    code = credentials.get("auth_code", "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9.-]+", email) or not re.fullmatch(r"[!-~]+", code):
        raise ConnectorError("not_logged_in", "请先绑定 QQ 邮箱地址及 IMAP 授权码")
    try:
        args: SearchArgs | ReadArgs
        if name == "search_messages":
            args = SearchArgs.model_validate(arguments)
        elif name == "read_message":
            args = ReadArgs.model_validate(arguments)
        else:
            raise ConnectorError("unknown_tool", "未知 QQ 邮箱工具")
    except ValidationError:
        raise ConnectorError("invalid_arguments", "请检查邮件搜索条件或来源 ID") from None
    return await asyncio.to_thread(_run, args, email, code)
