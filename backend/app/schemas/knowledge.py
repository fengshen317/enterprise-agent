"""知识库接口与工具契约，禁止暴露 RAGFlow 内部标识。"""

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CHUNKER_COMPONENT = "GeneralChunker:SixApplesFall"
PARSER_COMPONENT = "Parser:HipSignsRhyme"
EXTRACTOR_COMPONENT = "Extractor:AutoExtractDefault"
ALLOWED_PARSER_IDS = ("general", "manual")
KNOWLEDGE_TOOL = "search_knowledge_base"


def component_key(config: dict[str, Any], exact: str, prefix: str) -> str:
    """定位 RAGFlow 组件名；实例会给组件追加混淆后缀。

    Args:
        config: RAGFlow parser_config。
        exact: 已知的默认组件名。
        prefix: 组件类别前缀，例如 `Parser:`。

    Returns:
        实际存在的组件键；都不存在时返回默认键，用于新建。
    """
    if exact in config:
        return exact
    for key in config:
        if key.startswith(prefix):
            return key
    return exact


class ParserSettings(BaseModel):
    """应用可编辑的解析参数白名单；None 表示不修改对应组件。"""

    model_config = ConfigDict(extra="forbid")
    chunk_token_size: int = Field(default=512, ge=64, le=4096)
    delimiters: list[str] = Field(default_factory=lambda: ["\n"], min_length=1, max_length=20)
    overlapped_percent: float = Field(default=0.0, ge=0, le=0.5)
    use_parent_child: bool = False
    parent_child_delimiter: str = Field(default="\n", min_length=1, max_length=40)
    # 新字段默认 None：旧客户端不提交时绝不触碰 Parser/Extractor 组件。
    pdf_parse_method: Literal["deepdoc", "mineru"] | None = None
    auto_keywords_top_n: int | None = Field(default=None, ge=0, le=30)
    auto_questions_top_n: int | None = Field(default=None, ge=0, le=30)

    @field_validator("delimiters")
    @classmethod
    def validate_delimiters(cls, value: list[str]) -> list[str]:
        """拒绝空分隔符和超长分隔符，避免生成不可预测的切分规则。

        Args:
            value: 客户端提交的分隔符列表。

        Returns:
            去重后的分隔符列表。
        """
        cleaned: list[str] = []
        for item in value:
            delimiter = item.strip("\r")
            if not delimiter or len(delimiter) > 20:
                raise ValueError("分隔符不能为空且不能超过 20 个字符")
            if delimiter not in cleaned:
                cleaned.append(delimiter)
        if not cleaned:
            raise ValueError("至少需要一个分隔符")
        return cleaned

    def component_config(self) -> dict[str, Any]:
        """转换为 RAGFlow 1.0 的组件配置。

        Returns:
            分块组件始终输出；Parser/Extractor 仅在显式设置时输出。
        """
        config: dict[str, Any] = {
            CHUNKER_COMPONENT: {
                "chunk_token_size": self.chunk_token_size,
                "delimiters": list(self.delimiters),
                "overlapped_percent": self.overlapped_percent,
                "children_delimiters": [],
                "image_context_size": 0,
                "table_context_size": 0,
                "parent_child": {
                    "children_delimiter": self.parent_child_delimiter,
                    "use_parent_child": self.use_parent_child,
                },
            }
        }
        if self.pdf_parse_method is not None:
            config[PARSER_COMPONENT] = {"pdf": {"parse_method": self.pdf_parse_method}}
        extractor: dict[str, Any] = {}
        if self.auto_keywords_top_n is not None:
            extractor["keywords"] = {"top_n": self.auto_keywords_top_n}
        if self.auto_questions_top_n is not None:
            extractor["questions"] = {"top_n": self.auto_questions_top_n}
        if extractor:
            config[EXTRACTOR_COMPONENT] = extractor
        return config

    def merged_into(self, existing: dict[str, Any] | None) -> dict[str, Any]:
        """保留现有其它组件，只覆盖本应用允许修改的字段。

        Args:
            existing: RAGFlow 返回的完整 parser_config。

        Returns:
            可提交给 RAGFlow 的完整配置。
        """
        result = deepcopy(existing or {})
        chunker_key = component_key(result, CHUNKER_COMPONENT, "GeneralChunker:")
        component = result.get(chunker_key)
        if not isinstance(component, dict):
            component = {}
        component.update(self.component_config()[CHUNKER_COMPONENT])
        result[chunker_key] = component
        if self.pdf_parse_method is not None:
            parser_key = component_key(result, PARSER_COMPONENT, "Parser:")
            parser = result.get(parser_key)
            if not isinstance(parser, dict):
                parser = {}
            pdf = parser.get("pdf")
            if not isinstance(pdf, dict):
                pdf = {}
            pdf["parse_method"] = self.pdf_parse_method
            parser["pdf"] = pdf
            result[parser_key] = parser
        extractor_values: dict[str, Any] = {}
        if self.auto_keywords_top_n is not None:
            extractor_values["keywords"] = {"top_n": self.auto_keywords_top_n}
        if self.auto_questions_top_n is not None:
            extractor_values["questions"] = {"top_n": self.auto_questions_top_n}
        if extractor_values:
            extractor_key = component_key(result, EXTRACTOR_COMPONENT, "Extractor:")
            extractor = result.get(extractor_key)
            if not isinstance(extractor, dict):
                extractor = {}
            for name, value in extractor_values.items():
                current = extractor.get(name)
                if not isinstance(current, dict):
                    current = {}
                current.update(value)
                extractor[name] = current
            result[extractor_key] = extractor
        return result

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "ParserSettings":
        """从 RAGFlow 配置读取应用可编辑字段。

        Args:
            config: RAGFlow parser_config。

        Returns:
            带默认值的解析设置。
        """
        source = config or {}
        component = source.get(component_key(source, CHUNKER_COMPONENT, "GeneralChunker:"))
        parent = component.get("parent_child") if isinstance(component, dict) else None
        parent_dict = parent if isinstance(parent, dict) else {}
        delimiters = component.get("delimiters") if isinstance(component, dict) else None
        parser = source.get(component_key(source, PARSER_COMPONENT, "Parser:"))
        pdf = parser.get("pdf") if isinstance(parser, dict) else None
        pdf_dict = pdf if isinstance(pdf, dict) else {}
        raw_method = pdf_dict.get("parse_method")
        pdf_parse_method: Literal["deepdoc", "mineru"] | None = None
        if isinstance(raw_method, str) and raw_method.lower() == "deepdoc":
            pdf_parse_method = "deepdoc"
        elif isinstance(raw_method, str) and raw_method.lower() == "mineru":
            pdf_parse_method = "mineru"
        extractor = source.get(component_key(source, EXTRACTOR_COMPONENT, "Extractor:"))
        keywords = extractor.get("keywords") if isinstance(extractor, dict) else None
        questions = extractor.get("questions") if isinstance(extractor, dict) else None
        keywords_top_n = keywords.get("top_n") if isinstance(keywords, dict) else None
        questions_top_n = questions.get("top_n") if isinstance(questions, dict) else None
        return cls(
            chunk_token_size=int(component.get("chunk_token_size", 512)) if isinstance(component, dict) else 512,
            delimiters=list(delimiters) if isinstance(delimiters, list) and delimiters else ["\n"],
            overlapped_percent=float(component.get("overlapped_percent", 0.0)) if isinstance(component, dict) else 0.0,
            use_parent_child=bool(parent_dict.get("use_parent_child", False)),
            parent_child_delimiter=str(parent_dict.get("children_delimiter", "\n")),
            pdf_parse_method=pdf_parse_method,
            auto_keywords_top_n=keywords_top_n if isinstance(keywords_top_n, int) else None,
            auto_questions_top_n=questions_top_n if isinstance(questions_top_n, int) else None,
        )


class KnowledgeBaseInput(BaseModel):
    """创建与完整更新知识库的输入。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    visibility: Literal["workspace", "private"] = "workspace"
    parser_settings: ParserSettings = Field(default_factory=ParserSettings)
    parser_id: str = Field(default="", max_length=64)
    embedding_model: str = Field(default="", max_length=200)
    language: str = Field(default="", max_length=64)

    @field_validator("parser_id")
    @classmethod
    def validate_parser_id(cls, value: str) -> str:
        """只允许已验证的 RAGFlow 解析器；空串表示沿用配置默认值。

        Args:
            value: 客户端提交的 parser_id。

        Returns:
            去空白后的 parser_id。
        """
        cleaned = value.strip()
        if cleaned and cleaned not in ALLOWED_PARSER_IDS:
            raise ValueError("不支持的解析器")
        return cleaned

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        """拒绝只有空白的知识库名称。

        Args:
            value: 原始名称。

        Returns:
            去空白后的名称。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("知识库名称不能为空")
        return cleaned


class KnowledgeBaseView(BaseModel):
    """知识库公开视图，不包含 RAGFlow dataset/tenant ID。"""

    id: UUID
    name: str
    description: str
    visibility: str
    status: str
    error_code: str | None
    parser_settings: ParserSettings
    parser_id: str
    embedding_model: str
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentView(BaseModel):
    """文档公开视图，不包含 RAGFlow document ID。"""

    id: UUID
    kb_id: UUID
    name: str
    mime: str
    size: int
    sha256: str
    status: str
    error_code: str | None
    progress: float = 0.0
    error_message: str | None = None
    chunk_count: int
    token_count: int
    created_at: datetime
    updated_at: datetime


class DocumentParseInput(BaseModel):
    """可选的单文档解析覆盖；为空时使用知识库默认配置。"""

    model_config = ConfigDict(extra="forbid")
    parser_settings: ParserSettings | None = None


class KnowledgeSearchArgs(BaseModel):
    """模型只能提交查询条件和应用知识库 ID，不能提交数据集身份。"""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=5, ge=1, le=10)


class ListKnowledgeBasesArgs(BaseModel):
    """列出当前 Agent 可访问知识库，供模型选择检索范围。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=20, ge=1, le=50)


class KnowledgeChunk(BaseModel):
    """返回给模型的资料片段，附带可追溯的应用侧来源。"""

    content: str
    kb_id: UUID
    document_id: UUID
    document_name: str
    similarity: float | None = None
    positions: list[Any] = Field(default_factory=list)


class KnowledgeSearchResult(BaseModel):
    """检索结果，不返回 RAGFlow 原始 dataset/document ID。"""

    chunks: list[KnowledgeChunk]


class KnowledgeChunkItem(BaseModel):
    """只读分块条目，不暴露 RAGFlow chunk ID。"""

    content: str
    available: bool = True


class KnowledgeChunkPage(BaseModel):
    """分块分页结果。"""

    total: int
    offset: int
    limit: int
    chunks: list[KnowledgeChunkItem]


class RetrievalTestInput(BaseModel):
    """详情页检索测试输入；与 Agent 工具入参分离，允许用户调阈值。"""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    threshold: float = Field(default=0.2, ge=0, le=1)


class RetrievalTestChunk(BaseModel):
    """检索测试命中的片段，来源使用应用侧文档 ID。"""

    content: str
    document_id: UUID
    document_name: str
    similarity: float | None = None
    positions: list[Any] = Field(default_factory=list)


class RetrievalTestResult(BaseModel):
    """检索测试结果。"""

    chunks: list[RetrievalTestChunk]
