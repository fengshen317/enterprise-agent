"""技能与不可变版本的读写边界。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.identity import WorkspaceMembership
from app.models.knowledge import KnowledgeBase
from app.models.skill import Skill, SkillVersion
from app.schemas.auth import ResourceScope
from app.schemas.skill import SkillInput, SkillVersionView, SkillView
from app.services.auth import require_membership

if TYPE_CHECKING:
    from app.capabilities.registry import ToolRegistry


class SkillService:
    """技能正文与版本、可见性和绑定校验。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        registry: ToolRegistry,
    ) -> None:
        """保存会话工厂、配置和工具目录。

        Args:
            factory: 独立数据库会话工厂。
            settings: 技能预算与开关。
            registry: 内置工具目录。
        """
        self.factory = factory
        self.settings = settings
        self.registry = registry

    async def _role(self, session: AsyncSession, scope: ResourceScope) -> str:
        """复核成员并返回空间角色。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。

        Returns:
            admin 或 member。
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
        return role

    @staticmethod
    def _can_manage(skill: Skill, scope: ResourceScope, role: str) -> bool:
        """私有技能仅 owner 管理；空间技能管理员或 owner 管理。

        Args:
            skill: 技能记录。
            scope: 当前用户和空间。
            role: 空间角色。

        Returns:
            是否可管理。
        """
        if skill.visibility == "private":
            return skill.owner_user_id == scope.user_id
        return role == "admin" or skill.owner_user_id == scope.user_id

    async def list_skills(self, scope: ResourceScope, offset: int, limit: int) -> list[SkillView]:
        """列出空间技能与自己的私有技能。

        Args:
            scope: 可信用户和空间。
            offset: 分页偏移。
            limit: 页面大小。

        Returns:
            技能摘要列表。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            rows = list(
                await session.scalars(
                    select(Skill)
                    .where(
                        Skill.workspace_id == scope.workspace_id,
                        or_(
                            and_(Skill.visibility == "workspace", Skill.status == "published"),
                            Skill.owner_user_id == scope.user_id,
                        ),
                    )
                    .order_by(Skill.updated_at.desc(), Skill.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [await self._view(session, row, scope, role, include_content=False) for row in rows]

    async def get_skill(self, scope: ResourceScope, skill_id: UUID) -> SkillView:
        """读取技能详情与当前版本正文。

        Args:
            scope: 可信用户和空间。
            skill_id: 技能标识。

        Returns:
            技能详情。

        Raises:
            ApplicationError: 不存在或不可读。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            skill = await self._readable(session, scope, skill_id)
            return await self._view(session, skill, scope, role, include_content=True)

    async def create_skill(self, scope: ResourceScope, body: SkillInput) -> SkillView:
        """创建技能和第一版草稿。

        Args:
            scope: 可信用户和空间。
            body: 技能内容与绑定。

        Returns:
            新技能。

        Raises:
            ApplicationError: 权限、键名或绑定不合法。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            if body.visibility == "workspace" and role != "admin":
                raise ApplicationError("SKILL_ADMIN_REQUIRED", "只有空间管理员可以创建空间技能", 403)
            if await session.scalar(
                select(Skill.id).where(Skill.workspace_id == scope.workspace_id, Skill.key == body.key)
            ):
                raise ApplicationError("SKILL_KEY_EXISTS", "技能 key 已存在", 409)
            await self._validate_bindings(session, scope, body, visibility=body.visibility)
            skill = Skill(
                workspace_id=scope.workspace_id,
                owner_user_id=scope.user_id,
                key=body.key,
                name=body.name,
                description=body.description,
                visibility=body.visibility,
                status="draft",
                current_version=1,
            )
            session.add(skill)
            await session.flush()
            session.add(self._version(skill, 1, body, scope.user_id))
            await session.flush()
            return await self._view(session, skill, scope, role, include_content=True)

    async def update_skill(self, scope: ResourceScope, skill_id: UUID, body: SkillInput) -> SkillView:
        """新建草稿版本，不改变已绑定 Agent 的旧版本。

        Args:
            scope: 可信用户和空间。
            skill_id: 技能标识。
            body: 新版本内容。

        Returns:
            更新后的技能。

        Raises:
            ApplicationError: 不存在、无权管理或 key 被修改。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            skill = await self._readable(session, scope, skill_id)
            if not self._can_manage(skill, scope, role):
                raise ApplicationError("SKILL_ACCESS_DENIED", "无权修改该技能", 403)
            if body.key != skill.key:
                raise ApplicationError("SKILL_KEY_IMMUTABLE", "技能 key 创建后不能修改", 422)
            if body.visibility == "workspace" and role != "admin":
                raise ApplicationError("SKILL_ADMIN_REQUIRED", "只有空间管理员可以发布空间技能", 403)
            await self._validate_bindings(session, scope, body, visibility=body.visibility)
            next_version = skill.current_version + 1
            skill.name = body.name
            skill.description = body.description
            skill.visibility = body.visibility
            skill.status = "draft"
            skill.current_version = next_version
            skill.updated_at = datetime.now(UTC)
            session.add(self._version(skill, next_version, body, scope.user_id))
            await session.flush()
            return await self._view(session, skill, scope, role, include_content=True)

    async def publish_skill(self, scope: ResourceScope, skill_id: UUID, version: int) -> SkillView:
        """发布指定版本，使新 Agent 可以绑定。

        Args:
            scope: 可信用户和空间。
            skill_id: 技能标识。
            version: 目标版本号。

        Returns:
            发布后的技能。

        Raises:
            ApplicationError: 版本不存在或无权发布。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            skill = await self._readable(session, scope, skill_id)
            if not self._can_manage(skill, scope, role):
                raise ApplicationError("SKILL_ACCESS_DENIED", "无权发布该技能", 403)
            row = await session.scalar(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id, SkillVersion.version == version)
                .with_for_update()
            )
            if row is None:
                raise ApplicationError("SKILL_VERSION_NOT_FOUND", "技能版本不存在", 404)
            row.published_at = row.published_at or datetime.now(UTC)
            skill.current_version = version
            skill.status = "published"
            skill.updated_at = datetime.now(UTC)
            await session.flush()
            return await self._view(session, skill, scope, role, include_content=True)

    async def archive_skill(self, scope: ResourceScope, skill_id: UUID) -> SkillView:
        """归档技能；历史版本和已绑定 Agent 保留。

        Args:
            scope: 可信用户和空间。
            skill_id: 技能标识。

        Returns:
            归档后的技能。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            skill = await self._readable(session, scope, skill_id)
            if not self._can_manage(skill, scope, role):
                raise ApplicationError("SKILL_ACCESS_DENIED", "无权归档该技能", 403)
            skill.status = "archived"
            skill.updated_at = datetime.now(UTC)
            await session.flush()
            return await self._view(session, skill, scope, role, include_content=True)

    async def list_versions(self, scope: ResourceScope, skill_id: UUID) -> list[SkillVersionView]:
        """读取技能版本历史。

        Args:
            scope: 可信用户和空间。
            skill_id: 技能标识。

        Returns:
            按版本倒序的历史。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            await self._readable(session, scope, skill_id)
            rows = list(
                await session.scalars(
                    select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version.desc())
                )
            )
            return [
                SkillVersionView(
                    id=row.id,
                    version=row.version,
                    content_md=row.content_md,
                    content_sha256=row.content_sha256,
                    tool_keys=row.tool_keys,
                    knowledge_scope=row.knowledge_scope,
                    activation=row.activation,  # type: ignore[arg-type]
                    token_budget=row.token_budget,
                    change_note=row.change_note,
                    created_at=row.created_at,
                    published_at=row.published_at,
                )
                for row in rows
            ]

    async def _readable(self, session: AsyncSession, scope: ResourceScope, skill_id: UUID) -> Skill:
        """读取技能并应用 visible 规则。

        Args:
            session: 当前事务。
            scope: 当前用户和空间。
            skill_id: 技能标识。

        Returns:
            技能记录。

        Raises:
            ApplicationError: 不存在或不可读。
        """
        skill = await session.scalar(
            select(Skill).where(
                Skill.id == skill_id,
                Skill.workspace_id == scope.workspace_id,
                or_(Skill.visibility == "workspace", Skill.owner_user_id == scope.user_id),
            )
        )
        if skill is None:
            raise ApplicationError("SKILL_NOT_FOUND", "技能不存在", 404)
        return skill

    @staticmethod
    def _version(skill: Skill, version: int, body: SkillInput, user_id: UUID) -> SkillVersion:
        """构造不可变版本记录。

        Args:
            skill: 所属技能。
            version: 版本号。
            body: 技能输入。
            user_id: 创建者。

        Returns:
            新版本记录。
        """
        digest = hashlib.sha256(body.content_md.encode("utf-8")).hexdigest()
        return SkillVersion(
            skill_id=skill.id,
            version=version,
            content_md=body.content_md,
            content_sha256=digest,
            tool_keys=body.tool_keys,
            knowledge_scope=(
                {"mode": "all_readable"}
                if body.knowledge_mode == "all_readable"
                else {"mode": "selected", "kb_ids": [str(item) for item in body.knowledge_base_ids]}
            ),
            activation=body.activation,
            token_budget=body.token_budget,
            change_note=body.change_note,
            created_by_user_id=user_id,
        )

    async def _validate_bindings(
        self,
        session: AsyncSession,
        scope: ResourceScope,
        body: SkillInput,
        *,
        visibility: str,
    ) -> None:
        """校验技能声明的工具和知识库范围。

        Args:
            session: 当前事务。
            scope: 当前用户和空间。
            body: 技能输入。
            visibility: 技能可见性。

        Raises:
            ApplicationError: 工具或知识库不可用。
        """
        if len(body.tool_keys) > self.settings.agent_max_tools:
            raise ApplicationError("SKILL_TOOL_LIMIT", "技能声明的工具数量超过上限", 422)
        for key in body.tool_keys:
            spec = self.registry.resolve(key)
            if spec is None or spec.system:
                raise ApplicationError("TOOL_UNAVAILABLE", f"工具不可用：{key}", 422)
        if body.knowledge_mode == "selected" and not body.knowledge_base_ids:
            raise ApplicationError("SKILL_KNOWLEDGE_SCOPE", "指定知识库范围时至少选择一个知识库", 422)
        if body.knowledge_mode == "selected":
            rows = (
                await session.execute(
                    select(KnowledgeBase.id, KnowledgeBase.visibility, KnowledgeBase.owner_user_id).where(
                        KnowledgeBase.workspace_id == scope.workspace_id,
                        KnowledgeBase.id.in_(body.knowledge_base_ids),
                    )
                )
            ).all()
            if len(rows) != len(set(body.knowledge_base_ids)):
                raise ApplicationError("KNOWLEDGE_NOT_FOUND", "知识库不存在", 404)
            for _, kb_visibility, owner_user_id in rows:
                if kb_visibility == "private" and owner_user_id != scope.user_id:
                    raise ApplicationError("KNOWLEDGE_ACCESS_DENIED", "无权使用该知识库", 403)
                if visibility == "workspace" and kb_visibility != "workspace":
                    raise ApplicationError("SKILL_KNOWLEDGE_SCOPE", "空间技能只能绑定空间知识库", 422)

    async def _view(
        self,
        session: AsyncSession,
        skill: Skill,
        scope: ResourceScope,
        role: str,
        *,
        include_content: bool,
    ) -> SkillView:
        """组装技能视图，列表请求不返回正文。

        Args:
            session: 当前事务。
            skill: 技能记录。
            scope: 当前用户和空间。
            role: 空间角色。
            include_content: 是否返回 Markdown 正文。

        Returns:
            技能视图。
        """
        version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.version == skill.current_version,
            )
        )
        return SkillView(
            id=skill.id,
            key=skill.key,
            name=skill.name,
            description=skill.description,
            visibility=skill.visibility,  # type: ignore[arg-type]
            status=skill.status,  # type: ignore[arg-type]
            current_version=skill.current_version,
            owner_user_id=skill.owner_user_id,
            is_owner=skill.owner_user_id == scope.user_id,
            can_manage=self._can_manage(skill, scope, role),
            activation=version.activation if version else "always",  # type: ignore[arg-type]
            tool_keys=version.tool_keys if version else [],
            knowledge_scope=version.knowledge_scope if version else {"mode": "all_readable"},
            token_budget=version.token_budget if version else 4000,
            content_md=version.content_md if version and include_content else None,
            created_at=skill.created_at,
            updated_at=skill.updated_at,
        )
