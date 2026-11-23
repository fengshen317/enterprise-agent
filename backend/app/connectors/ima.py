"""腾讯 ima 只读 OpenAPI，协议依据官方 ima Skill 1.1.9。"""

from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.connectors.base import ConnectorError, ConnectorToolDef

_BASE_URL = "https://ima.qq.com/openapi/"
_PAGE_SIZE = 20
_MAX_ID = 1024
_MAX_QUERY = 500
_MAX_SNIPPET = 500
_MAX_TITLE = 256
_MAX_CONTENT = 6000
_TIMEOUT = 15
_NOTE_MEDIA_TYPE = 11
_TEXT_FORMAT = 0
_ID = Annotated[str, Field(min_length=1, max_length=_MAX_ID)]
_Query = Annotated[str, Field(min_length=1, max_length=_MAX_QUERY)]
_Limit = Annotated[int, Field(ge=1, le=_PAGE_SIZE)]
_Cursor = Annotated[str, Field(max_length=_MAX_ID)]


class _Args(BaseModel):
    """只接受工具声明的参数，凭证独立于模型输入。"""

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)


class _Bases(_Args):
    """列举或搜索知识库。"""

    query: Annotated[str, Field(max_length=_MAX_QUERY)] = ""
    cursor: _Cursor = ""
    limit: _Limit = _PAGE_SIZE


class _Entries(_Args):
    """搜索指定知识库中的条目。"""

    query: _Query
    knowledge_base_id: _ID
    cursor: _Cursor = ""


class _Media(_Args):
    """定位知识条目对应的笔记。"""

    media_id: _ID


class _Notes(_Args):
    """按正文或标题检索个人笔记。"""

    query: _Query
    by_title: bool = False
    start: Annotated[int, Field(ge=0, le=2**63 - 1 - _PAGE_SIZE)] = 0
    limit: _Limit = _PAGE_SIZE


class _Content(_Args):
    """获取作者本人的笔记纯文本。"""

    note_id: _ID


_OPERATIONS: dict[str, tuple[str, type[_Args], str]] = {
    "search_knowledge_base": ("wiki", _Bases, "按名称搜索 ima 知识库，空关键词列出可见知识库。"),
    "search_knowledge": ("wiki", _Entries, "在一个 ima 知识库内搜索条目，返回媒体 ID 和匹配片段。"),
    "get_media_info": ("wiki", _Media, "查看 ima 条目的媒体类型；笔记类型返回可继续读取的笔记 ID。"),
    "search_note": ("note", _Notes, "按正文搜索个人 ima 笔记；by_title=true 时按标题搜索。"),
    "get_doc_content": ("note", _Content, "使用笔记 ID 读取当前用户所写 ima 笔记的纯文本。"),
}

TOOLS: tuple[ConnectorToolDef, ...] = tuple(
    ConnectorToolDef(name, description, model.model_json_schema(), risk="read", approval="never")
    for name, (_, model, description) in _OPERATIONS.items()
)


def _invalid_output(rule: str, *, status: int | None = None, code: int | None = None) -> ConnectorError:
    """构造可安全展示的协议错误。

    Args:
        rule: 内部校验规则名称。
        status: 上游 HTTP 状态码。
        code: 上游业务码。

    Returns:
        不包含上游正文的异常。
    """
    details: dict[str, str | int] = {"phase": "response_validation", "rule": rule}
    if status is not None:
        details["upstream_status"] = status
    if code is not None:
        details["upstream_code"] = code
    return ConnectorError("invalid_output", "ima 返回的数据格式异常", diagnostics=details)


def _project(data: dict[str, Any], args: _Args) -> dict[str, object]:
    """仅投影可引用的标识、标题与有限长度文本。

    Args:
        data: 已成功解包的业务数据。
        args: 已验证的工具参数。

    Returns:
        带来源和分页或截断信息的模型可见结果。
    """
    result: dict[str, object] = {"source": "腾讯 ima"}
    if isinstance(args, _Content):
        content = data.get("content")
        if not isinstance(content, str):
            raise _invalid_output("note_content")
        return {
            **result,
            "note_id": args.note_id,
            "content": content[:_MAX_CONTENT],
            "truncated": len(content) > _MAX_CONTENT,
        }
    if isinstance(args, _Media):
        media_type = data.get("media_type")
        if type(media_type) is not int:
            raise _invalid_output("media_type")
        result.update(media_id=args.media_id, media_type=media_type)
        # 上游 url_info 可能附有临时签名和 Authorization，模型只需要媒体和笔记标识。
        if media_type == _NOTE_MEDIA_TYPE:
            note = data.get("notebook_ext_info")
            if not isinstance(note, dict) or not _valid_id(note.get("notebook_id")):
                raise _invalid_output("note_identity")
            result["note_id"] = note["notebook_id"]
        return result

    if isinstance(args, _Notes):
        list_key, id_key, title_key, snippet_key = "search_note_infos", "note_id", "title", "summary"
    elif isinstance(args, _Bases):
        list_key, id_key, title_key, snippet_key = "info_list", "id", "name", ""
    else:
        list_key, id_key, title_key, snippet_key = "info_list", "media_id", "title", "highlight_content"
    # protobuf 的空列表可省略；未知结构不能被当作有内容的搜索命中。
    rows = data.get(list_key, [])
    if not isinstance(rows, list):
        raise _invalid_output("knowledge_list")
    cap = args.limit if isinstance(args, (_Bases, _Notes)) else _PAGE_SIZE
    items: list[dict[str, str]] = []
    for row in rows[:cap]:
        if isinstance(args, _Notes) and isinstance(row, dict):
            row = row.get("note_book_info")
        if isinstance(args, _Bases) and isinstance(row, dict):
            # 线上字段与文档不同，统一为现有工具输出的 id 和 name。
            row = {"id": row.get("kb_id", row.get("id")), "name": row.get("kb_name", row.get("name"))}
        if not isinstance(row, dict) or not _valid_id(row.get(id_key)):
            raise _invalid_output("knowledge_base_id" if isinstance(args, _Bases) else "row_identity")
        item = {id_key: row[id_key]}
        for field, maximum in ((title_key, _MAX_TITLE), (snippet_key, _MAX_SNIPPET)):
            value = row.get(field)
            if field and isinstance(value, str):
                item[field] = value[:maximum]
        items.append(item)
    result.update(items=items, truncated=len(rows) > cap)
    if isinstance(args, _Entries):
        result["knowledge_base_id"] = args.knowledge_base_id
    is_end = data.get("is_end")
    if isinstance(is_end, bool):
        result["is_end"] = is_end
    if isinstance(args, _Notes):
        if is_end is False:
            result["next_start"] = args.start + cap
    elif is_end is not True and _valid_id(data.get("next_cursor")):
        result["next_cursor"] = data["next_cursor"]
    return result


def _valid_id(value: object) -> bool:
    """检查可传入下一次请求的标识或游标。

    Args:
        value: 上游标识。

    Returns:
        是否为完整的非空有界字符串。
    """
    return isinstance(value, str) and 0 < len(value) <= _MAX_ID


async def invoke(name: str, arguments: dict[str, object], credentials: dict[str, str]) -> object:
    """使用当次调用用户的凭证执行只读操作。

    Args:
        name: 官方只读操作名。
        arguments: 模型提供的工具参数。
        credentials: 当前用户的 client_id 和 api_key。

    Returns:
        带来源的公开字段。

    Raises:
        ConnectorError: 工具、参数、授权、传输或上游协议异常。
    """
    operation = _OPERATIONS.get(name)
    if operation is None:
        raise ConnectorError("unknown_tool", "未知的 ima 只读工具")
    namespace, model, _ = operation
    try:
        args = model.model_validate(arguments)
    except ValidationError:
        raise ConnectorError("invalid_arguments", "ima 工具参数无效") from None
    body = args.model_dump()
    if isinstance(args, _Notes):
        body = {
            "query_info": {"title" if args.by_title else "content": args.query},
            "search_type": 0 if args.by_title else 1,
            "start": args.start,
            "end": args.start + args.limit,
        }
    elif isinstance(args, _Content):
        body["target_content_format"] = _TEXT_FORMAT
    client_id, api_key = credentials.get("client_id", ""), credentials.get("api_key", "")
    if not all(
        value and value.isascii() and value.isprintable() and value == value.strip() for value in (client_id, api_key)
    ):
        raise ConnectorError("auth_failed", "请检查个人 ima 凭证")
    headers = {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "ima-openapi-ctx": "skill_version=1.1.9",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = await client.post(f"{_BASE_URL}{namespace}/v1/{name}", headers=headers, json=body)
    except httpx.TimeoutException:
        raise ConnectorError("timeout", "ima 请求超时", diagnostics={"phase": "transport"}) from None
    except httpx.HTTPError:
        raise ConnectorError("network_error", "ima 连接失败", diagnostics={"phase": "transport"}) from None
    status = response.status_code
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise ConnectorError(
            "auth_failed", "请检查个人 ima 凭证", diagnostics={"phase": "auth", "upstream_status": status}
        )
    if response.status_code == httpx.codes.FORBIDDEN:
        raise ConnectorError(
            "permission_denied", "个人 ima 账号缺少访问权限", diagnostics={"phase": "auth", "upstream_status": status}
        )
    if not response.is_success:
        raise ConnectorError(
            "upstream_error", "ima 请求失败", diagnostics={"phase": "upstream_business", "upstream_status": status}
        )
    try:
        envelope = response.json()
    except (ValueError, RecursionError):
        raise _invalid_output("invalid_json", status=status) from None
    if not isinstance(envelope, dict) or type(envelope.get("code")) is not int:
        raise _invalid_output("response_envelope", status=status)
    code = envelope["code"]
    if code in {110030, 210005, 210011}:
        raise ConnectorError(
            "permission_denied",
            "个人 ima 账号缺少访问权限",
            diagnostics={"phase": "auth", "upstream_status": status, "upstream_code": code},
        )
    if code != 0:
        raise ConnectorError(
            "upstream_error",
            "ima 无法完成此次读取",
            diagnostics={"phase": "upstream_business", "upstream_status": status, "upstream_code": code},
        )
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise _invalid_output("response_data", status=status, code=code)
    try:
        return _project(data, args)
    except ConnectorError as exc:
        raise ConnectorError(
            exc.code, exc.message, diagnostics={**exc.diagnostics, "upstream_status": status, "upstream_code": code}
        ) from None
