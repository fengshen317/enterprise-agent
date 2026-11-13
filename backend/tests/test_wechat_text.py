"""微信纯文本排版。"""

from app.channels.wechat_text import markdown_to_plain, progress_label, wechat_reply


def test_markdown_becomes_readable_plain_text() -> None:
    """标题、强调、表格和代码块都不再保留标记。"""
    source = "# 标题\n\n**重点**\n\n| 列 | 值 |\n| --- | --- |\n| 名称 | 序列 |\n\n```\nprint(1)\n```\n\n详见 [文档](https://example.test)。"
    plain = markdown_to_plain(source)
    assert "标题" in plain
    assert "#" not in plain
    assert "**" not in plain
    assert "名称、值" in plain or "名称、序列" in plain
    assert "print(1)" in plain
    assert "```" not in plain
    assert "文档（https://example.test）" in plain


def test_long_reply_keeps_a_page_link() -> None:
    """超过预览长度时附上网页地址。"""
    text = wechat_reply("甲" * 100, preview_characters=40, page_url="http://127.0.0.1:8686/chat/abc")
    assert text.endswith("http://127.0.0.1:8686/chat/abc")
    assert "完整回答见：" in text
    assert len(text) < 100


def test_progress_label_names_knowledge_search() -> None:
    """知识库工具用固定短句。"""
    assert progress_label("search_knowledge_base") == "正在查知识库…"
    assert progress_label(None) == "仍在处理…"
