"""知识库只读工具：检索与目录，权限由运行身份和应用 ACL 裁剪。"""

from pydantic import BaseModel

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.knowledge import KNOWLEDGE_TOOL, KnowledgeSearchArgs, ListKnowledgeBasesArgs


def knowledge_specs(settings: Settings) -> list[ToolSpec]:
    """构造知识库工具声明。

    Args:
        settings: 超时、配额和上下文限制。

    Returns:
        检索与目录两个只读工具。
    """

    async def search(context: ToolContext, arguments: BaseModel) -> object:
        """按 Agent 范围与用户 ACL 求交集后检索。

        Args:
            context: 可信运行上下文。
            arguments: 检索参数。

        Returns:
            带应用侧来源的片段。
        """
        if not isinstance(arguments, KnowledgeSearchArgs):
            raise TypeError("知识库检索参数类型不正确")
        allowed = context.assembly.selected_kb_ids() if context.assembly else None
        if allowed is not None:
            requested = set(arguments.kb_ids) if arguments.kb_ids else allowed
            selected = requested & allowed
            if not selected:
                raise ApplicationError("KNOWLEDGE_ACCESS_DENIED", "没有可用的知识库", 403)
            arguments = arguments.model_copy(update={"kb_ids": list(selected)})
        result = await context.executor._knowledge(arguments)
        return result.model_dump(mode="json")

    def search_audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """审计只保存查询摘要和来源标识，不保存正文。

        Args:
            arguments: 检索参数。
            result: 检索结果。

        Returns:
            脱敏审计摘要。
        """
        assert isinstance(arguments, KnowledgeSearchArgs)
        data = result if isinstance(result, dict) else {}
        chunks = data.get("chunks", [])
        rows = chunks if isinstance(chunks, list) else []
        return {
            "query": arguments.query[:200],
            "kb_ids": [str(item) for item in arguments.kb_ids],
            "chunks": [
                {
                    "document_id": row.get("document_id"),
                    "document_name": row.get("document_name"),
                    "similarity": row.get("similarity"),
                }
                for row in rows
                if isinstance(row, dict)
            ],
        }

    async def listing(context: ToolContext, arguments: BaseModel) -> object:
        """列出 Agent 范围内可访问的知识库。

        Args:
            context: 可信运行上下文。
            arguments: 条数限制。

        Returns:
            不含 RAGFlow 标识的知识库摘要。
        """
        if not isinstance(arguments, ListKnowledgeBasesArgs):
            raise TypeError("知识库目录参数类型不正确")
        if context.knowledge_service is None:
            raise ApplicationError("KNOWLEDGE_DISABLED", "知识库功能未启用", 503)
        views = await context.knowledge_service.list_bases(context.scope, 0, arguments.limit)
        allowed = context.assembly.selected_kb_ids() if context.assembly else None
        if allowed is not None:
            views = [view for view in views if view.id in allowed]
        return {"knowledge_bases": [view.model_dump(mode="json") for view in views]}

    def list_audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """只记录目录规模和名称，不保存知识库正文。

        Args:
            arguments: 条数限制。
            result: 目录结果。

        Returns:
            脱敏审计摘要。
        """
        assert isinstance(arguments, ListKnowledgeBasesArgs)
        data = result if isinstance(result, dict) else {}
        rows = data.get("knowledge_bases", [])
        items = rows if isinstance(rows, list) else []
        return {
            "count": len(items),
            "names": [row.get("name") for row in items if isinstance(row, dict)],
        }

    return [
        ToolSpec(
            key=KNOWLEDGE_TOOL,
            version=1,
            display_name="知识库检索",
            description=(
                "检索当前用户当前空间可访问的知识库，返回带来源的片段。"
                "适合需要企业制度、产品资料或文档依据的问题；kb_ids 可省略。"
            ),
            args_model=KnowledgeSearchArgs,
            risk="read",
            approval="never",
            timeout_seconds=settings.ragflow_timeout_seconds + settings.tool_timeout_seconds,
            quota_per_run=settings.knowledge_max_calls_per_run,
            handler=search,
            audit=search_audit,
        ),
        ToolSpec(
            key="list_knowledge_bases",
            version=1,
            display_name="知识库目录",
            description="列出当前 Agent 可访问的应用知识库及文档统计，用于选择检索范围。",
            args_model=ListKnowledgeBasesArgs,
            risk="read",
            approval="never",
            timeout_seconds=settings.tool_timeout_seconds,
            quota_per_run=2,
            handler=listing,
            audit=list_audit,
        ),
    ]
