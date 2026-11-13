"""把模型 Markdown 收成适合微信阅读的纯文本。"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_TABLE_SPLIT = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$", re.MULTILINE)


def markdown_to_plain(markdown: str) -> str:
    """去掉标题和强调，保留代码、链接和表格的可读形式。

    Args:
        markdown: 模型原文。

    Returns:
        纯文本。
    """
    text = markdown.replace("\r\n", "\n")
    text = _FENCE.sub(lambda match: "\n" + match.group(1).strip("\n") + "\n", text)
    text = _IMAGE.sub(lambda match: match.group(1), text)
    text = _LINK.sub(lambda match: f"{match.group(1)}（{match.group(2)}）", text)
    text = _HEADING.sub("", text)
    text = _BOLD.sub(lambda match: match.group(2), text)
    text = _ITALIC.sub(lambda match: match.group(1), text)
    text = _TABLE_SPLIT.sub("", text)
    lines = [_table_line(line) for line in text.split("\n")]
    compact = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", compact).strip()


def wechat_reply(markdown: str, *, preview_characters: int, page_url: str) -> str:
    """过长回答只发开头，并附上网页里的完整对话。

    Args:
        markdown: 模型原文。
        preview_characters: 微信里保留的最大字符数。
        page_url: 网页对话地址。

    Returns:
        可直接发送的文本。
    """
    plain = markdown_to_plain(markdown)
    if not plain:
        return ""
    if len(plain) <= preview_characters:
        return plain
    cut = plain.rfind("\n", 0, preview_characters)
    if cut < preview_characters // 2:
        cut = preview_characters
    head = plain[:cut].rstrip()
    return f"{head}\n\n内容较长，完整回答见：{page_url}"


def progress_label(tool_name: str | None) -> str:
    """把正在执行的工具说成一句短进度。

    Args:
        tool_name: 工具标识；没有工具时为空。

    Returns:
        发给微信的进度句。
    """
    if not tool_name:
        return "仍在处理…"
    lowered = tool_name.lower()
    if "knowledge" in lowered:
        return "正在查知识库…"
    if "web_search" in lowered or "tavily" in lowered:
        return "正在联网搜索…"
    if "schedule" in lowered:
        return "正在处理定时任务…"
    return f"正在调用 {tool_name}…"


def _table_line(line: str) -> str:
    """把一行 Markdown 表格收成用顿号隔开的单元格。

    Args:
        line: 单行文本。

    Returns:
        普通文本行。
    """
    stripped = line.strip()
    if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        cells = [cell for cell in cells if cell and not re.fullmatch(r":?-{3,}:?", cell)]
        if cells:
            return "、".join(cells)
    return line
