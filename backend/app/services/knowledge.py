"""知识库权限、文档状态与 RAGFlow 调用编排。"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.identity import WorkspaceMembership
from app.models.knowledge import KnowledgeBase, KnowledgeDocument
from app.schemas.auth import ResourceScope
from app.schemas.knowledge import (
    KnowledgeBaseInput,
    KnowledgeBaseView,
    KnowledgeChunk,
    KnowledgeChunkItem,
    KnowledgeChunkPage,
    KnowledgeDocumentView,
    KnowledgeSearchArgs,
    KnowledgeSearchResult,
    ParserSettings,
    RetrievalTestChunk,
    RetrievalTestInput,
    RetrievalTestResult,
)
from app.services.auth import require_membership
from app.services.ragflow import DatasetInfo, DocumentFile, DocumentInfo, RagflowClientProtocol

logger = logging.getLogger(__name__)

RETRIEVAL_TEST_CHUNK_CHARACTERS = 2000
REPARSE_BATCH_SIZE = 50


@dataclass(frozen=True)
class DocumentDownload:
    """鉴权后的原文件下载上下文。"""

    view: KnowledgeDocumentView
    file: DocumentFile


ALLOWED_SUFFIXES = {
    "pdf",
    "doc",
    "docx",
    "txt",
    "md",
    "markdown",
    "xls",
    "xlsx",
    "csv",
    "ppt",
    "pptx",
    "html",
    "htm",
    "eml",
    "msg",
}
RAGFLOW_PARSE_COMPLETED = "COMPLETED"


class KnowledgeService:
    """应用侧权限与状态为事实来源，RAGFlow 只负责解析和检索。"""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], settings: Settings, client: RagflowClientProtocol
    ) -> None:
        """绑定会话工厂、配置与 RAGFlow 客户端。

        Args:
            factory: 独立数据库会话工厂。
            settings: 知识库开关与检索限制。
            client: RAGFlow 适配器，测试可注入替身。
        """
        self.factory = factory
        self.settings = settings
        self.client = client

    @staticmethod
    def _kb_view(kb: KnowledgeBase, document_count: int = 0, chunk_count: int = 0) -> KnowledgeBaseView:
        """把 ORM 记录转换为不暴露 RAGFlow ID 的公开视图。

        Args:
            kb: 知识库记录。
            document_count: 当前文档数。
            chunk_count: 当前累计 chunk 数。

        Returns:
            公开视图。
        """
        return KnowledgeBaseView(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            visibility=kb.visibility,
            status=kb.status,
            error_code=kb.error_code,
            parser_settings=ParserSettings.from_config(kb.parser_config),
            parser_id=kb.parser_id,
            embedding_model=kb.embedding_model,
            document_count=document_count,
            chunk_count=chunk_count,
            created_at=kb.created_at,
            updated_at=kb.updated_at,
        )

    @staticmethod
    def _document_view(doc: KnowledgeDocument) -> KnowledgeDocumentView:
        """把文档记录转换为不暴露 RAGFlow ID 的公开视图。

        Args:
            doc: 文档记录。

        Returns:
            公开视图。
        """
        return KnowledgeDocumentView(
            id=doc.id,
            kb_id=doc.kb_id,
            name=doc.name,
            mime=doc.mime,
            size=doc.size,
            sha256=doc.sha256,
            status=doc.status,
            error_code=doc.error_code,
            progress=doc.progress,
            error_message=doc.error_message,
            chunk_count=doc.chunk_count,
            token_count=doc.token_count,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )

    async def _role(self, session: AsyncSession, scope: ResourceScope) -> str:
        """复核成员资格并返回空间角色。

        Args:
            session: 独立会话。
            scope: 可信用户和空间。

        Returns:
            `admin` 或 `member`。
        """
        await require_membership(session, scope)
        role = await session.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.workspace_id == scope.workspace_id,
                WorkspaceMembership.user_id == scope.user_id,
                WorkspaceMembership.status == "active",
            )
        )
        if role is None:
            raise ApplicationError("WORKSPACE_ACCESS_DENIED", "无权访问该工作空间", 403)
        return str(role)

    @staticmethod
    def _require_read(kb: KnowledgeBase, scope: ResourceScope) -> None:
        """私有知识库对其他用户统一返回不存在。

        Args:
            kb: 知识库记录。
            scope: 当前可信身份。
        """
        if kb.visibility == "private" and kb.owner_user_id != scope.user_id:
            raise ApplicationError("KNOWLEDGE_NOT_FOUND", "知识库不存在", 404)

    @staticmethod
    def _require_manage(kb: KnowledgeBase, scope: ResourceScope, role: str) -> None:
        """私有库仅创建者可管理；空间库允许管理员或创建者管理。

        Args:
            kb: 知识库记录。
            scope: 当前可信身份。
            role: 当前空间角色。
        """
        if kb.visibility == "private":
            if kb.owner_user_id != scope.user_id:
                raise ApplicationError("KNOWLEDGE_NOT_FOUND", "知识库不存在", 404)
            return
        if role != "admin" and kb.owner_user_id != scope.user_id:
            raise ApplicationError("KNOWLEDGE_FORBIDDEN", "没有管理该知识库的权限", 403)

    async def _kb(self, session: AsyncSession, scope: ResourceScope, kb_id: UUID) -> KnowledgeBase:
        """按空间加载知识库，不存在或不属当前空间统一返回 404。

        Args:
            session: 当前会话。
            scope: 可信用户和空间。
            kb_id: 知识库标识。

        Returns:
            知识库记录。
        """
        kb = await session.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.workspace_id == scope.workspace_id,
            )
        )
        if kb is None:
            raise ApplicationError("KNOWLEDGE_NOT_FOUND", "知识库不存在", 404)
        return kb

    async def _document(self, session: AsyncSession, scope: ResourceScope, document_id: UUID) -> KnowledgeDocument:
        """加载当前空间文档，不信任前端传入的知识库归属。

        Args:
            session: 当前会话。
            scope: 可信用户和空间。
            document_id: 文档标识。

        Returns:
            文档记录。
        """
        doc = await session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.workspace_id == scope.workspace_id,
            )
        )
        if doc is None:
            raise ApplicationError("DOCUMENT_NOT_FOUND", "文档不存在", 404)
        return doc

    async def _kb_stats(
        self, session: AsyncSession, scope: ResourceScope, kb_ids: list[UUID]
    ) -> dict[UUID, tuple[int, int]]:
        """实时聚合知识库的文档数与 chunk 数。

        Args:
            session: 当前会话。
            scope: 可信用户和空间。
            kb_ids: 待统计的知识库。

        Returns:
            `kb_id -> (document_count, chunk_count)`。
        """
        if not kb_ids:
            return {}
        rows = (
            await session.execute(
                select(
                    KnowledgeDocument.kb_id,
                    func.count(KnowledgeDocument.id),
                    func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0),
                )
                .where(
                    KnowledgeDocument.workspace_id == scope.workspace_id,
                    KnowledgeDocument.kb_id.in_(kb_ids),
                )
                .group_by(KnowledgeDocument.kb_id)
            )
        ).all()
        return {kb_id: (int(count), int(chunks)) for kb_id, count, chunks in rows}

    async def create(self, scope: ResourceScope, body: KnowledgeBaseInput) -> KnowledgeBaseView:
        """先创建应用记录，再创建 RAGFlow dataset 并写入配置。

        Args:
            scope: 可信用户和空间。
            body: 知识库输入。

        Returns:
            已就绪的知识库。
        """
        if not self.settings.knowledge_enabled:
            raise ApplicationError("KNOWLEDGE_DISABLED", "知识库功能未启用", 503)
        parser_id = body.parser_id or self.settings.ragflow_parser_id
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            if body.visibility == "workspace" and role != "admin":
                raise ApplicationError("KNOWLEDGE_FORBIDDEN", "仅空间管理员可以创建空间知识库", 403)
            kb = KnowledgeBase(
                workspace_id=scope.workspace_id,
                owner_user_id=scope.user_id,
                name=body.name,
                description=body.description,
                visibility=body.visibility,
                status="creating",
                parser_id=parser_id,
                parse_type=self.settings.ragflow_parse_type,
                parser_config=body.parser_settings.component_config(),
                embedding_model=body.embedding_model or self.settings.ragflow_embedding_model,
            )
            session.add(kb)
            await session.flush()
            kb_id = kb.id
        created: DatasetInfo | None = None
        try:
            created = await self.client.create_dataset(
                body.name,
                parser_id,
                self.settings.ragflow_parse_type,
                body.embedding_model or self.settings.ragflow_embedding_model,
                body.language or self.settings.ragflow_language,
            )
            config = body.parser_settings.merged_into(created.parser_config)
            await self.client.update_dataset(
                created.dataset_id,
                parser_id,
                self.settings.ragflow_parse_type,
                config,
                body.embedding_model or self.settings.ragflow_embedding_model,
            )
        except ApplicationError as exc:
            await self._discard_dataset(created)
            await self._kb_failed(kb_id, exc.code)
            raise
        except Exception as exc:
            await self._discard_dataset(created)
            await self._kb_failed(kb_id, "KNOWLEDGE_CREATE_FAILED")
            logger.error("knowledge_create_failed", extra={"kb_id": str(kb_id), "error_type": type(exc).__name__})
            raise ApplicationError("KNOWLEDGE_CREATE_FAILED", "知识库创建失败，请重试", 502) from exc
        async with self.factory.begin() as session:
            stored = await session.get(KnowledgeBase, kb_id)
            if stored is None:
                raise ApplicationError("KNOWLEDGE_NOT_FOUND", "知识库不存在", 404)
            stored.status = "ready"
            stored.ragflow_dataset_id = created.dataset_id
            stored.parser_config = config
            stored.error_code = None
            stored.updated_at = datetime.now(UTC)
            await session.flush()
            return self._kb_view(stored)

    async def list_bases(self, scope: ResourceScope, offset: int, limit: int) -> list[KnowledgeBaseView]:
        """列出当前用户可读的知识库。

        Args:
            scope: 可信用户和空间。
            offset: 分页偏移。
            limit: 页面大小。

        Returns:
            空间知识库与自己的私有知识库。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            rows = list(
                await session.scalars(
                    select(KnowledgeBase)
                    .where(
                        KnowledgeBase.workspace_id == scope.workspace_id,
                        or_(
                            KnowledgeBase.visibility == "workspace",
                            KnowledgeBase.owner_user_id == scope.user_id,
                        ),
                    )
                    .order_by(KnowledgeBase.updated_at.desc(), KnowledgeBase.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            stats = await self._kb_stats(session, scope, [row.id for row in rows])
            return [self._kb_view(row, *stats.get(row.id, (0, 0))) for row in rows]

    async def get(self, scope: ResourceScope, kb_id: UUID) -> KnowledgeBaseView:
        """读取知识库详情。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。

        Returns:
            公开视图。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_read(kb, scope)
            stats = await self._kb_stats(session, scope, [kb.id])
            return self._kb_view(kb, *stats.get(kb.id, (0, 0)))

    async def update(self, scope: ResourceScope, kb_id: UUID, body: KnowledgeBaseInput) -> KnowledgeBaseView:
        """更新知识库；解析配置变化时同步 RAGFlow。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。
            body: 完整更新输入。

        Returns:
            更新后的公开视图。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_manage(kb, scope, role)
            if body.visibility == "workspace" and role != "admin":
                raise ApplicationError("KNOWLEDGE_FORBIDDEN", "仅空间管理员可以发布空间知识库", 403)
            if body.embedding_model and body.embedding_model != kb.embedding_model:
                raise ApplicationError("EMBEDDING_MODEL_IMMUTABLE", "首版不支持修改 embedding 模型", 409)
            previous_config = dict(kb.parser_config or {})
            dataset_id = kb.ragflow_dataset_id
            parser_id = body.parser_id or kb.parser_id
            parse_type = kb.parse_type
            new_config = body.parser_settings.merged_into(previous_config)
            config_changed = new_config != previous_config
            parser_changed = parser_id != kb.parser_id
        if (config_changed or parser_changed) and dataset_id:
            await self.client.update_dataset(dataset_id, parser_id, parse_type, new_config)
        async with self.factory.begin() as session:
            kb = await self._kb(session, scope, kb_id)
            kb.name = body.name
            kb.description = body.description
            kb.visibility = body.visibility
            kb.parser_id = parser_id
            if config_changed:
                kb.parser_config = new_config
                kb.config_version += 1
            kb.updated_at = datetime.now(UTC)
            await session.flush()
            stats = await self._kb_stats(session, scope, [kb.id])
            return self._kb_view(kb, *stats.get(kb.id, (0, 0)))

    async def delete(self, scope: ResourceScope, kb_id: UUID) -> None:
        """删除知识库；RAGFlow 删除失败时保留失败状态。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_manage(kb, scope, role)
            dataset_id = kb.ragflow_dataset_id
            kb.status = "deleting"
            kb.updated_at = datetime.now(UTC)
        if dataset_id:
            try:
                await self.client.delete_dataset(dataset_id)
            except ApplicationError as exc:
                async with self.factory.begin() as session:
                    await session.execute(
                        update(KnowledgeBase)
                        .where(KnowledgeBase.id == kb_id)
                        .values(status="delete_failed", error_code=exc.code, updated_at=datetime.now(UTC))
                    )
                raise
        async with self.factory.begin() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))

    async def upload(
        self,
        scope: ResourceScope,
        kb_id: UUID,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> KnowledgeDocumentView:
        """上传后立即写文档级解析配置并触发解析。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。
            filename: 文件名。
            content: 文件内容。
            content_type: MIME 类型。

        Returns:
            已进入解析状态的文档。
        """
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in ALLOWED_SUFFIXES:
            raise ApplicationError("KNOWLEDGE_FILE_TYPE", "不支持该文件类型", 422)
        if len(content) > self.settings.knowledge_max_file_bytes:
            raise ApplicationError("KNOWLEDGE_FILE_TOO_LARGE", "文件超过允许大小", 413)
        digest = hashlib.sha256(content).hexdigest()
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_manage(kb, scope, role)
            if kb.status != "ready" or not kb.ragflow_dataset_id:
                raise ApplicationError("KNOWLEDGE_NOT_READY", "知识库尚未就绪", 409)
            dataset_id = kb.ragflow_dataset_id
            parser_id = kb.parser_id
            parse_type = kb.parse_type
            config = ParserSettings.from_config(kb.parser_config).merged_into(kb.parser_config)
            doc = KnowledgeDocument(
                kb_id=kb.id,
                workspace_id=scope.workspace_id,
                uploaded_by_user_id=scope.user_id,
                name=filename,
                mime=content_type or "application/octet-stream",
                size=len(content),
                sha256=digest,
                status="uploading",
                parse_config=config,
            )
            session.add(doc)
            await session.flush()
            document_id = doc.id
        try:
            uploaded = await self.client.upload_document(dataset_id, filename, content, content_type)
            await self._doc_update(
                document_id,
                ragflow_document_id=uploaded.document_id,
                status="uploaded",
            )
            effective = ParserSettings.from_config(config).merged_into(uploaded.parser_config or config)
            await self.client.patch_document_parser(
                dataset_id,
                uploaded.document_id,
                parser_id,
                parse_type,
                effective,
            )
            await self._doc_update(document_id, status="configuring", parse_config=effective)
            await self.client.parse_documents(dataset_id, [uploaded.document_id])
            await self._doc_update(document_id, status="parsing")
        except ApplicationError as exc:
            await self._doc_update(document_id, status="failed", error_code=exc.code)
            raise
        except Exception as exc:
            await self._doc_update(document_id, status="failed", error_code="KNOWLEDGE_UPLOAD_FAILED")
            logger.error(
                "knowledge_upload_failed",
                extra={"document_id": str(document_id), "error_type": type(exc).__name__},
            )
            raise ApplicationError("KNOWLEDGE_UPLOAD_FAILED", "文档上传失败，请重试", 502) from exc
        return await self.get_document(scope, document_id)

    async def list_documents(self, scope: ResourceScope, kb_id: UUID) -> list[KnowledgeDocumentView]:
        """读取本地文档并尽力同步 RAGFlow 解析状态。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。

        Returns:
            文档列表。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_read(kb, scope)
            dataset_id = kb.ragflow_dataset_id
        if dataset_id:
            try:
                remote = await self.client.list_documents(dataset_id)
                await self._sync_documents(scope, kb_id, remote)
            except ApplicationError:
                # 列表仍返回本地状态，外部服务恢复后再次刷新即可。
                logger.warning("knowledge_status_refresh_failed", extra={"kb_id": str(kb_id)})
        async with self.factory() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeDocument)
                    .where(
                        KnowledgeDocument.kb_id == kb_id,
                        KnowledgeDocument.workspace_id == scope.workspace_id,
                    )
                    .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc())
                )
            )
            return [self._document_view(row) for row in rows]

    async def get_document(self, scope: ResourceScope, document_id: UUID) -> KnowledgeDocumentView:
        """读取单文档公开状态。

        Args:
            scope: 可信用户和空间。
            document_id: 文档标识。

        Returns:
            文档公开视图。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            doc = await self._document(session, scope, document_id)
            kb = await self._kb(session, scope, doc.kb_id)
            self._require_read(kb, scope)
            return self._document_view(doc)

    async def parse(
        self, scope: ResourceScope, document_id: UUID, parser_settings: ParserSettings | None
    ) -> KnowledgeDocumentView:
        """重新写配置并触发解析；失败保留可重试状态。

        Args:
            scope: 可信用户和空间。
            document_id: 文档标识。
            parser_settings: 可选本次解析覆盖。

        Returns:
            文档公开视图。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            doc = await self._document(session, scope, document_id)
            kb = await self._kb(session, scope, doc.kb_id)
            self._require_manage(kb, scope, role)
            if not kb.ragflow_dataset_id or not doc.ragflow_document_id:
                raise ApplicationError("DOCUMENT_NOT_READY", "文档尚未上传完成", 409)
            dataset_id = kb.ragflow_dataset_id
            remote_document_id = doc.ragflow_document_id
            parser_id = kb.parser_id
            parse_type = kb.parse_type
            # 重新解析默认使用知识库当前配置；显式覆盖只影响本次文档。
            base = parser_settings.merged_into(kb.parser_config) if parser_settings else kb.parser_config
        try:
            await self.client.patch_document_parser(dataset_id, remote_document_id, parser_id, parse_type, base)
            await self._doc_update(
                document_id,
                status="configuring",
                parse_config=base,
                error_code=None,
                error_message=None,
                progress=0.0,
            )
            await self.client.parse_documents(dataset_id, [remote_document_id])
            await self._doc_update(document_id, status="parsing")
        except ApplicationError as exc:
            await self._doc_update(document_id, status="failed", error_code=exc.code)
            raise
        return await self.get_document(scope, document_id)

    async def delete_document(self, scope: ResourceScope, document_id: UUID) -> None:
        """删除 RAGFlow 文档，成功后移除本地元数据。

        Args:
            scope: 可信用户和空间。
            document_id: 文档标识。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            doc = await self._document(session, scope, document_id)
            kb = await self._kb(session, scope, doc.kb_id)
            self._require_manage(kb, scope, role)
            dataset_id = kb.ragflow_dataset_id
            remote_document_id = doc.ragflow_document_id
        if dataset_id and remote_document_id:
            try:
                await self.client.delete_documents(dataset_id, [remote_document_id])
            except ApplicationError as exc:
                await self._doc_update(document_id, status="delete_failed", error_code=exc.code)
                raise
        async with self.factory.begin() as session:
            await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id))

    async def open_document_file(self, scope: ResourceScope, document_id: UUID) -> DocumentDownload:
        """校验读权限后打开 RAGFlow 原文件流。

        Args:
            scope: 可信用户和空间。
            document_id: 文档标识。

        Returns:
            文档视图与流式文件。

        Raises:
            ApplicationError: 文档不存在、未就绪或原文件超过大小限制。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            doc = await self._document(session, scope, document_id)
            kb = await self._kb(session, scope, doc.kb_id)
            self._require_read(kb, scope)
            if not kb.ragflow_dataset_id or not doc.ragflow_document_id:
                raise ApplicationError("DOCUMENT_NOT_READY", "文档尚未上传完成", 409)
            dataset_id = kb.ragflow_dataset_id
            remote_document_id = doc.ragflow_document_id
            view = self._document_view(doc)
        file = await self.client.download_document(dataset_id, remote_document_id)
        if file.content_length is not None and file.content_length > self.settings.knowledge_max_file_bytes:
            await file.aclose()
            raise ApplicationError("KNOWLEDGE_FILE_TOO_LARGE", "文件超过允许大小", 413)
        return DocumentDownload(view=view, file=file)

    async def list_document_chunks(
        self, scope: ResourceScope, document_id: UUID, offset: int, limit: int
    ) -> KnowledgeChunkPage:
        """读取文档分块只读列表。

        Args:
            scope: 可信用户和空间。
            document_id: 文档标识。
            offset: 起始偏移。
            limit: 每页条数。

        Returns:
            分块分页结果。
        """
        if offset < 0 or limit < 1:
            raise ApplicationError("INVALID_PAGINATION", "分页参数不正确", 422)
        async with self.factory() as session:
            await self._role(session, scope)
            doc = await self._document(session, scope, document_id)
            kb = await self._kb(session, scope, doc.kb_id)
            self._require_read(kb, scope)
            if not kb.ragflow_dataset_id or not doc.ragflow_document_id:
                raise ApplicationError("DOCUMENT_NOT_READY", "文档尚未上传完成", 409)
            dataset_id = kb.ragflow_dataset_id
            remote_document_id = doc.ragflow_document_id
        page = offset // limit + 1
        result = await self.client.list_chunks(dataset_id, remote_document_id, page, limit)
        return KnowledgeChunkPage(
            total=result.total,
            offset=offset,
            limit=limit,
            chunks=[KnowledgeChunkItem(content=item.content, available=item.available) for item in result.chunks],
        )

    async def search_test(self, scope: ResourceScope, kb_id: UUID, args: RetrievalTestInput) -> RetrievalTestResult:
        """面向登录用户的检索测试，不写工具审计、不做上下文预算裁剪。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。
            args: 用户指定的查询、条数与阈值。

        Returns:
            带来源与相似度的命中片段。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_read(kb, scope)
            if kb.status != "ready" or not kb.ragflow_dataset_id:
                raise ApplicationError("KNOWLEDGE_NOT_READY", "知识库尚未就绪", 409)
            dataset_id = kb.ragflow_dataset_id
        top_k = min(args.top_k, self.settings.knowledge_test_top_k)
        chunks = await self.client.retrieve([dataset_id], args.query, top_k, args.threshold)
        documents = await self._documents_by_remote_id(scope, [kb_id], [chunk.document_id for chunk in chunks])
        result: list[RetrievalTestChunk] = []
        for chunk in chunks:
            document = documents.get(chunk.document_id)
            if document is None:
                continue
            result.append(
                RetrievalTestChunk(
                    content=chunk.content[:RETRIEVAL_TEST_CHUNK_CHARACTERS],
                    document_id=document[0],
                    document_name=document[1],
                    similarity=chunk.similarity,
                    positions=chunk.positions,
                )
            )
        return RetrievalTestResult(chunks=result)

    async def reparse_all(self, scope: ResourceScope, kb_id: UUID) -> list[KnowledgeDocumentView]:
        """把知识库当前配置批量应用到已有文档并触发重新解析。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。

        Returns:
            刷新后的文档列表。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            kb = await self._kb(session, scope, kb_id)
            self._require_manage(kb, scope, role)
            if kb.status != "ready" or not kb.ragflow_dataset_id:
                raise ApplicationError("KNOWLEDGE_NOT_READY", "知识库尚未就绪", 409)
            dataset_id = kb.ragflow_dataset_id
            parser_config = dict(kb.parser_config or {})
            rows = (
                await session.execute(
                    select(KnowledgeDocument.id, KnowledgeDocument.ragflow_document_id).where(
                        KnowledgeDocument.kb_id == kb_id,
                        KnowledgeDocument.workspace_id == scope.workspace_id,
                        KnowledgeDocument.ragflow_document_id.is_not(None),
                    )
                )
            ).all()
        pairs = [(row[0], row[1]) for row in rows if isinstance(row[1], str)]
        if not pairs:
            return await self.list_documents(scope, kb_id)
        document_ids = [item[0] for item in pairs]
        remote_ids = [item[1] for item in pairs]
        try:
            await self.client.apply_dataset_parser_config(dataset_id)
            await self._doc_update_many(
                document_ids,
                parse_config=parser_config,
                parse_config_version=KnowledgeDocument.parse_config_version + 1,
                status="configuring",
                error_code=None,
                error_message=None,
                progress=0.0,
            )
            for start in range(0, len(remote_ids), REPARSE_BATCH_SIZE):
                await self.client.parse_documents(dataset_id, remote_ids[start : start + REPARSE_BATCH_SIZE])
            await self._doc_update_many(document_ids, status="parsing")
        except ApplicationError as exc:
            await self._doc_update_many(document_ids, status="failed", error_code=exc.code)
            raise
        return await self.list_documents(scope, kb_id)

    async def _documents_by_remote_id(
        self, scope: ResourceScope, kb_ids: list[UUID], remote_ids: list[str]
    ) -> dict[str, tuple[UUID, str, UUID]]:
        """把 RAGFlow 文档 ID 映射回可访问的应用侧文档。

        Args:
            scope: 可信用户和空间。
            kb_ids: 允许的知识库。
            remote_ids: RAGFlow 文档 ID。

        Returns:
            `remote_id -> (document_id, document_name, kb_id)`。
        """
        if not remote_ids:
            return {}
        async with self.factory() as session:
            rows = (
                await session.execute(
                    select(
                        KnowledgeDocument.ragflow_document_id,
                        KnowledgeDocument.id,
                        KnowledgeDocument.name,
                        KnowledgeDocument.kb_id,
                    ).where(
                        KnowledgeDocument.workspace_id == scope.workspace_id,
                        KnowledgeDocument.kb_id.in_(kb_ids),
                        KnowledgeDocument.ragflow_document_id.in_(remote_ids),
                    )
                )
            ).all()
        return {
            remote_id: (document_id, name, kb_id)
            for remote_id, document_id, name, kb_id in rows
            if isinstance(remote_id, str)
        }

    async def retrieve(self, scope: ResourceScope, args: KnowledgeSearchArgs) -> KnowledgeSearchResult:
        """按当前用户和空间裁剪 dataset，再调用 RAGFlow 检索。

        Args:
            scope: 可信用户和空间。
            args: 模型提供的查询条件和可选知识库 ID。

        Returns:
            只包含可访问来源的片段。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            rows = (
                await session.execute(
                    select(KnowledgeBase.id, KnowledgeBase.ragflow_dataset_id).where(
                        KnowledgeBase.workspace_id == scope.workspace_id,
                        KnowledgeBase.status == "ready",
                        KnowledgeBase.ragflow_dataset_id.is_not(None),
                        or_(
                            KnowledgeBase.visibility == "workspace",
                            KnowledgeBase.owner_user_id == scope.user_id,
                        ),
                    )
                )
            ).all()
        allowed = {kb_id: dataset_id for kb_id, dataset_id in rows if isinstance(dataset_id, str) and dataset_id}
        if args.kb_ids:
            requested = set(args.kb_ids)
            allowed = {kb_id: dataset_id for kb_id, dataset_id in allowed.items() if kb_id in requested}
        if not allowed:
            raise ApplicationError("KNOWLEDGE_ACCESS_DENIED", "没有可用的知识库", 403)
        dataset_ids = list(allowed.values())[:20]
        reverse = {dataset_id: kb_id for kb_id, dataset_id in allowed.items()}
        top_k = min(args.top_k, self.settings.knowledge_top_k)
        chunks = await self.client.retrieve(
            dataset_ids,
            args.query,
            top_k,
            self.settings.knowledge_similarity_threshold,
        )
        documents = await self._documents_by_remote_id(scope, list(allowed), [chunk.document_id for chunk in chunks])
        result: list[KnowledgeChunk] = []
        remaining = self.settings.knowledge_max_context_characters
        for chunk in chunks:
            kb_id = reverse.get(chunk.dataset_id)
            document = documents.get(chunk.document_id)
            if kb_id is None or document is None or remaining <= 0:
                continue
            content = chunk.content[:remaining]
            result.append(
                KnowledgeChunk(
                    content=content,
                    kb_id=document[2],
                    document_id=document[0],
                    document_name=document[1],
                    similarity=chunk.similarity,
                    positions=chunk.positions,
                )
            )
            remaining -= len(content)
        return KnowledgeSearchResult(chunks=result)

    async def _doc_update(self, document_id: UUID, **values: object) -> None:
        """在一个短事务中更新文档状态。

        Args:
            document_id: 文档标识。
            values: 待更新字段。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id == document_id)
                .values(**values, updated_at=datetime.now(UTC))
            )

    async def _doc_update_many(self, document_ids: list[UUID], **values: object) -> None:
        """在一个短事务中批量更新同一次重解析的文档。

        Args:
            document_ids: 文档标识列表。
            values: 待更新字段。
        """
        if not document_ids:
            return
        async with self.factory.begin() as session:
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id.in_(document_ids))
                .values(**values, updated_at=datetime.now(UTC))
            )

    async def _discard_dataset(self, created: DatasetInfo | None) -> None:
        """创建知识库中途失败时尽力清理已创建的 RAGFlow dataset。

        Args:
            created: 已创建的 dataset；创建本身失败时为空。
        """
        if created is None:
            return
        try:
            await self.client.delete_dataset(created.dataset_id)
        except ApplicationError:
            logger.warning("knowledge_cleanup_failed", extra={"dataset_id": created.dataset_id})

    async def _kb_failed(self, kb_id: UUID, code: str) -> None:
        """创建失败时保留可诊断状态，不删除用户刚建立的记录。

        Args:
            kb_id: 知识库标识。
            code: 稳定错误码。
        """
        async with self.factory.begin() as session:
            await session.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(status="failed", error_code=code, updated_at=datetime.now(UTC))
            )

    async def _sync_documents(self, scope: ResourceScope, kb_id: UUID, remote: list[DocumentInfo]) -> None:
        """将 RAGFlow 状态映射到本地文档。

        Args:
            scope: 可信用户和空间。
            kb_id: 知识库标识。
            remote: RAGFlow 文档状态。
        """
        async with self.factory.begin() as session:
            rows = {
                row.ragflow_document_id: row
                for row in await session.scalars(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.kb_id == kb_id,
                        KnowledgeDocument.workspace_id == scope.workspace_id,
                        KnowledgeDocument.ragflow_document_id.is_not(None),
                    )
                )
            }
            now = datetime.now(UTC)
            for item in remote:
                doc = rows.get(item.document_id)
                if doc is None:
                    continue
                if item.status == RAGFLOW_PARSE_COMPLETED:
                    doc.status = "ready"
                    doc.error_code = None
                    doc.error_message = None
                elif item.status == "FAIL":
                    doc.status = "failed"
                    doc.error_code = "RAGFLOW_PARSE_FAILED"
                    doc.error_message = (item.error or "")[:500] or None
                elif item.status == "CANCEL":
                    doc.status = "failed"
                    doc.error_code = "RAGFLOW_PARSE_CANCELLED"
                    doc.error_message = "解析已取消"
                elif item.status in ("RUNNING", "UNSTART") and doc.status in ("parsing", "configuring"):
                    doc.status = "parsing"
                doc.progress = item.progress
                doc.chunk_count = item.chunk_count
                doc.token_count = item.token_count
                doc.updated_at = now
            await session.flush()
