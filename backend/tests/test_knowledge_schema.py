"""知识库解析配置映射的纯单元验证。"""

import pytest

from app.schemas.knowledge import (
    CHUNKER_COMPONENT,
    EXTRACTOR_COMPONENT,
    PARSER_COMPONENT,
    KnowledgeBaseInput,
    ParserSettings,
)


def test_default_settings_merge_preserves_other_components() -> None:
    """只覆盖分块组件，不破坏 RAGFlow 其它解析组件。"""
    existing = {
        "Parser:HipSignsRhyme": {"pdf": {"parse_method": "DeepDOC"}},
        CHUNKER_COMPONENT: {"chunk_token_size": 512, "delimiters": ["\n"], "overlapped_percent": 0.0},
    }
    merged = ParserSettings(chunk_token_size=128, delimiters=["。", "\n"], overlapped_percent=0.05).merged_into(
        existing
    )
    assert merged["Parser:HipSignsRhyme"]["pdf"]["parse_method"] == "DeepDOC"
    assert merged[CHUNKER_COMPONENT]["chunk_token_size"] == 128
    assert merged[CHUNKER_COMPONENT]["delimiters"] == ["。", "\n"]
    assert merged[CHUNKER_COMPONENT]["overlapped_percent"] == 0.05


def test_from_config_round_trip() -> None:
    """从 RAGFlow 配置回填应用可编辑字段。"""
    config = ParserSettings(
        chunk_token_size=256,
        delimiters=["\n", "；"],
        overlapped_percent=0.1,
        use_parent_child=True,
        parent_child_delimiter="\n\n",
    ).component_config()
    parsed = ParserSettings.from_config(config)
    assert parsed.chunk_token_size == 256
    assert parsed.delimiters == ["\n", "；"]
    assert parsed.overlapped_percent == 0.1
    assert parsed.use_parent_child is True
    assert parsed.parent_child_delimiter == "\n\n"


def test_from_config_defaults_when_missing() -> None:
    """缺失配置时使用安全默认值。"""
    parsed = ParserSettings.from_config({})
    assert parsed.chunk_token_size == 512
    assert parsed.delimiters == ["\n"]


def test_advanced_fields_round_trip_and_preserve_siblings() -> None:
    """PDF 解析方式与自动提取只覆盖目标字段，保留同级组件。"""
    existing = {
        CHUNKER_COMPONENT: {"chunk_token_size": 512, "delimiters": ["\n"]},
        PARSER_COMPONENT: {
            "pdf": {"parse_method": "deepdoc", "mineru_lang": "Chinese"},
            "spreadsheet": {"parse_method": "deepdoc"},
        },
        EXTRACTOR_COMPONENT: {
            "keywords": {"top_n": 3},
            "metadata": {"enabled": True},
            "tags": {"top_n": 2},
        },
    }
    merged = ParserSettings(
        pdf_parse_method="mineru",
        auto_keywords_top_n=6,
        auto_questions_top_n=4,
    ).merged_into(existing)
    assert merged[PARSER_COMPONENT]["pdf"]["parse_method"] == "mineru"
    assert merged[PARSER_COMPONENT]["pdf"]["mineru_lang"] == "Chinese"
    assert merged[PARSER_COMPONENT]["spreadsheet"]["parse_method"] == "deepdoc"
    assert merged[EXTRACTOR_COMPONENT]["keywords"]["top_n"] == 6
    assert merged[EXTRACTOR_COMPONENT]["questions"]["top_n"] == 4
    assert merged[EXTRACTOR_COMPONENT]["metadata"]["enabled"] is True
    assert merged[EXTRACTOR_COMPONENT]["tags"]["top_n"] == 2
    parsed = ParserSettings.from_config(merged)
    assert parsed.pdf_parse_method == "mineru"
    assert parsed.auto_keywords_top_n == 6
    assert parsed.auto_questions_top_n == 4


def test_none_advanced_fields_leave_components_untouched() -> None:
    """旧客户端不提交新字段时，不创建也不修改 Parser/Extractor。"""
    existing = {
        CHUNKER_COMPONENT: {"chunk_token_size": 512, "delimiters": ["\n"]},
        PARSER_COMPONENT: {"pdf": {"parse_method": "deepdoc"}},
        EXTRACTOR_COMPONENT: {"keywords": {"top_n": 3}},
    }
    merged = ParserSettings(chunk_token_size=128).merged_into(existing)
    assert merged[PARSER_COMPONENT] == existing[PARSER_COMPONENT]
    assert merged[EXTRACTOR_COMPONENT] == existing[EXTRACTOR_COMPONENT]


def test_parser_id_whitelist() -> None:
    """只允许已验证的解析器，空串表示沿用配置默认。"""
    assert KnowledgeBaseInput(name="测试").parser_id == ""
    assert KnowledgeBaseInput(name="测试", parser_id="manual").parser_id == "manual"
    with pytest.raises(ValueError):
        KnowledgeBaseInput(name="测试", parser_id="naive")


def test_from_config_accepts_legacy_uppercase_method() -> None:
    """历史配置里的 DeepDOC 大写需归一化，其他值回退 None。"""
    parsed = ParserSettings.from_config({PARSER_COMPONENT: {"pdf": {"parse_method": "DeepDOC"}}})
    assert parsed.pdf_parse_method == "deepdoc"
    unknown = ParserSettings.from_config({PARSER_COMPONENT: {"pdf": {"parse_method": "other"}}})
    assert unknown.pdf_parse_method is None


@pytest.mark.parametrize(
    "settings",
    [
        {"delimiters": []},
        {"delimiters": [""]},
        {"delimiters": ["x" * 21]},
        {"chunk_token_size": 63},
        {"chunk_token_size": 4097},
        {"overlapped_percent": 0.6},
        {"pdf_parse_method": "other"},
        {"auto_keywords_top_n": 31},
        {"auto_questions_top_n": -1},
    ],
)
def test_invalid_parser_settings_are_rejected(settings: dict[str, object]) -> None:
    """越界配置必须在应用层拒绝，不直接透传给 RAGFlow。"""
    with pytest.raises(ValueError):
        ParserSettings(**settings)
