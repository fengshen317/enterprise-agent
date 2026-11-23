"""读写个人材料；原始文件、检索和 Agent 共用同一归属条件。"""

from uuid import UUID, uuid5

from sqlalchemy import Select, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from app.core.errors import ApplicationError
from app.models.conversation import ChatAttachment
from app.schemas.auth import ResourceScope
from app.schemas.chat import ChatAttachmentView
from app.schemas.materials import MaterialPage, MaterialText, MaterialView
from app.services.auth import require_membership
from app.services.chat_attachments import MAX_MESSAGE_FILE_BYTES, PreparedAttachment

EXCERPT_LENGTH = 320
READ_LENGTH = 3000
MAX_TERMS = 6


def personal_materials(scope: ResourceScope) -> Select[tuple[ChatAttachment]]:
    """限制为当前用户在当前空间归档的材料。"""
    return select(ChatAttachment).where(
        ChatAttachment.workspace_id == scope.workspace_id, ChatAttachment.owner_user_id == scope.user_id,
    )


def material_namespace(scope: ResourceScope) -> UUID:
    """让同一用户同一空间上传的同名同内容文件复用原件。"""
    return uuid5(scope.workspace_id, str(scope.user_id))


def attachment_view(row: ChatAttachment) -> ChatAttachmentView:
    """构造历史消息只需保存的轻量材料引用。"""
    return ChatAttachmentView(
        id=row.id, filename=row.filename, media_type=row.media_type, size_bytes=row.size_bytes,
        text_available=bool(row.extracted_text), text_truncated=row.text_truncated,
    )


def material_view(row: ChatAttachment, keywords: tuple[str, ...] = ()) -> MaterialView:
    """只在有检索词时返回截取的正文片段。"""
    excerpt = ""
    if keywords and row.extracted_text:
        normalized = row.extracted_text.lower()
        locations = [index for term in keywords if (index := normalized.find(term)) >= 0]
        start = max(0, min(locations, default=0) - 50)
        excerpt = row.extracted_text[start:start + EXCERPT_LENGTH].strip()
    return MaterialView(
        **attachment_view(row).model_dump(), created_at=row.created_at,
        source_conversation_id=row.conversation_id, excerpt=excerpt,
    )


class MaterialStore:
    """个人材料的来源记录和正文读取不需要加载原始二进制。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """复用应用的异步会话工厂。"""
        self.factory = factory

    @staticmethod
    async def persist(
        session: AsyncSession, scope: ResourceScope, ids: list[UUID], files: list[PreparedAttachment],
        source_conversation_id: UUID | None = None,
    ) -> list[ChatAttachment]:
        """在调用方事务中保存材料，并在返回前复核每个引用。"""
        # 不同对话可以同时发送同一文件，按稳定顺序插入并处理主键冲突。
        for item in sorted(files, key=lambda upload: upload.view.id):
            await session.execute(insert(ChatAttachment).values(
                id=item.view.id, workspace_id=scope.workspace_id, owner_user_id=scope.user_id,
                conversation_id=source_conversation_id, filename=item.view.filename,
                size_bytes=item.view.size_bytes, media_type=item.view.media_type, data=item.data,
                extracted_text=item.text, text_truncated=item.view.text_truncated,
            ).on_conflict_do_nothing(index_elements=[ChatAttachment.id]))
        rows = {row.id: row for row in await session.scalars(personal_materials(scope)
            .options(defer(ChatAttachment.data)).where(ChatAttachment.id.in_(ids)))}
        if not all(key in rows for key in ids):
            raise ApplicationError("CHAT_ATTACHMENT_NOT_FOUND", "未找到当前工作空间的个人材料", 404)
        if sum(rows[key].size_bytes for key in ids) > MAX_MESSAGE_FILE_BYTES:
            raise ApplicationError("CHAT_ATTACHMENTS_TOO_LARGE", "本次附件合计最多 16 MB", 413)
        return [rows[key] for key in ids]

    async def upload(self, scope: ResourceScope, files: list[PreparedAttachment]) -> list[MaterialView]:
        """直接归档材料，归档不需要创建对话或运行。"""
        async with self.factory.begin() as session:
            await require_membership(session, scope, write=True)
            rows = await self.persist(session, scope, [item.view.id for item in files], files)
            return [material_view(row) for row in rows]

    async def listing(self, scope: ResourceScope, query: str, offset: int, limit: int) -> MaterialPage:
        """以文件名和提取的文字做关键词匹配；空查询返回最近材料。"""
        if "\x00" in query:
            raise ApplicationError("INVALID_MATERIAL_QUERY", "检索词包含无效字符", 422)
        words = tuple(dict.fromkeys(query.lower().split()[:MAX_TERMS]))
        statement = personal_materials(scope).options(defer(ChatAttachment.data))
        if words:
            filename = func.lower(ChatAttachment.filename)
            body = func.lower(ChatAttachment.extracted_text)
            name_matches = [func.strpos(filename, word) > 0 for word in words]
            matches = [or_(name, func.strpos(body, word) > 0) for word, name in zip(words, name_matches, strict=True)]
            score = sum((case((match, 1), else_=0) for match in matches), start=literal(0))
            statement = statement.where(or_(*matches)).order_by(
                case((or_(*name_matches), 1), else_=0).desc(), score.desc(),
            )
        statement = statement.order_by(ChatAttachment.created_at.desc(), ChatAttachment.id.desc())
        async with self.factory() as session:
            await require_membership(session, scope)
            rows = list(await session.scalars(statement.offset(offset).limit(limit + 1)))
            return MaterialPage(items=[material_view(row, words) for row in rows[:limit]], has_more=len(rows) > limit)

    async def get(self, scope: ResourceScope, material_id: UUID, *, original: bool = False) -> ChatAttachment:
        """按标识读取本人材料，缺失和越权均返回不存在。"""
        async with self.factory() as session:
            await require_membership(session, scope)
            query = personal_materials(scope).where(ChatAttachment.id == material_id)
            row = await session.scalar(query if original else query.options(defer(ChatAttachment.data)))
            if row is None:
                raise ApplicationError("MATERIAL_NOT_FOUND", "未找到个人材料", 404)
            return row

    async def read_text(self, scope: ResourceScope, material_id: UUID, offset: int) -> MaterialText:
        """分段返回提取的原文，供 Agent 深入核对检索结果。"""
        row = await self.get(scope, material_id)
        end = offset + READ_LENGTH
        return MaterialText(
            material=material_view(row), text=row.extracted_text[offset:end],
            next_offset=end if end < len(row.extracted_text) else None,
        )
