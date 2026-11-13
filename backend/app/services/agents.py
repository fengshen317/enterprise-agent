"""Agent 配置、能力绑定与权限校验。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.agent import AgentCapability, AgentProfile, WorkspaceToolPolicy
from app.models.conversation import Conversation, Run
from app.models.identity import WorkspaceMembership
from app.models.knowledge import KnowledgeBase
from app.models.skill import Skill, SkillVersion
from app.schemas.agent import (
    AgentInput,
    AgentUpdate,
    AgentView,
    SkillBindingView,
    ToolBindingView,
)
from app.schemas.auth import ResourceScope
from app.schemas.chat import ConversationView
from app.schemas.tools import ToolView
from app.services.auth import owned_conversations, require_membership

if TYPE_CHECKING:
    from app.capabilities.registry import ToolRegistry


class AgentService:
    """Agent 配置的读写边界；所有可见性与绑定校验都在服务端完成。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        registry: ToolRegistry,
    ) -> None:
        """保存会话工厂、配置和工具目录。

        Args:
            factory: 独立数据库会话工厂。
            settings: 能力预算与默认模型。
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

        Raises:
            ApplicationError: 成员或空间不可用。
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
    def _visible(scope: ResourceScope) -> ColumnElement[bool]:
        """空间 Agent 全员可读，私有 Agent 仅 owner 可读。

        Args:
            scope: 可信用户和空间。

        Returns:
            SQLAlchemy 过滤表达式。
        """
        return or_(AgentProfile.visibility == "workspace", AgentProfile.owner_user_id == scope.user_id)

    @staticmethod
    def _can_manage(agent: AgentProfile, scope: ResourceScope, role: str) -> bool:
        """私有 Agent 仅 owner 管理；空间 Agent 管理员或 owner 管理。

        Args:
            agent: 目标配置。
            scope: 当前用户和空间。
            role: 空间角色。

        Returns:
            是否可管理。
        """
        if agent.visibility == "private":
            return agent.owner_user_id == scope.user_id
        return role == "admin" or agent.owner_user_id == scope.user_id

    async def list_agents(self, scope: ResourceScope, offset: int, limit: int) -> list[AgentView]:
        """列出空间 Agent 与自己的私有 Agent。

        Args:
            scope: 可信用户和空间。
            offset: 分页偏移。
            limit: 页面大小。

        Returns:
            Agent 视图列表。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            rows = list(
                await session.scalars(
                    select(AgentProfile)
                    .where(
                        AgentProfile.workspace_id == scope.workspace_id,
                        AgentProfile.status == "active",
                        self._visible(scope),
                    )
                    .order_by(AgentProfile.updated_at.desc(), AgentProfile.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [await self._view(session, row, scope, role) for row in rows]

    async def get_agent(self, scope: ResourceScope, agent_id: UUID) -> AgentView:
        """读取可访问的 Agent 详情。

        Args:
            scope: 可信用户和空间。
            agent_id: Agent 标识。

        Returns:
            Agent 视图。

        Raises:
            ApplicationError: 不存在或不可读。
        """
        async with self.factory() as session:
            role = await self._role(session, scope)
            agent = await session.scalar(
                select(AgentProfile).where(
                    AgentProfile.id == agent_id,
                    AgentProfile.workspace_id == scope.workspace_id,
                    self._visible(scope),
                )
            )
            if agent is None:
                raise ApplicationError("AGENT_NOT_FOUND", "Agent 不存在", 404)
            return await self._view(session, agent, scope, role)

    async def create_agent(self, scope: ResourceScope, body: AgentInput) -> AgentView:
        """创建 Agent 并绑定工具、技能和知识库范围。

        Args:
            scope: 可信用户和空间。
            body: 完整配置。

        Returns:
            已持久化的 Agent。

        Raises:
            ApplicationError: 权限或绑定不合法。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            if body.visibility == "workspace" and role != "admin":
                raise ApplicationError("AGENT_ADMIN_REQUIRED", "只有空间管理员可以创建空间 Agent", 403)
            await self._validate_bindings(session, scope, body)
            agent = AgentProfile(
                workspace_id=scope.workspace_id,
                owner_user_id=scope.user_id,
                name=body.name,
                description=body.description,
                icon=body.icon,
                instructions=body.instructions,
                visibility=body.visibility,
                status=body.status,
                model=body.model or self.settings.deepseek_model,
                allow_planning=body.allow_planning,
                allow_delegation=body.allow_delegation,
                knowledge_scope=self._knowledge_scope(body),
                starter_prompts=body.starter_prompts,
            )
            session.add(agent)
            await session.flush()
            await self._replace_capabilities(session, agent, body)
            await session.flush()
            return await self._view(session, agent, scope, role)

    async def update_agent(self, scope: ResourceScope, agent_id: UUID, body: AgentUpdate) -> AgentView:
        """完整更新 Agent，并用版本号防止覆盖并发修改。

        Args:
            scope: 可信用户和空间。
            agent_id: Agent 标识。
            body: 完整配置与期望版本。

        Returns:
            更新后的 Agent。

        Raises:
            ApplicationError: 不存在、无权管理或版本冲突。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            agent = await session.scalar(
                select(AgentProfile)
                .where(
                    AgentProfile.id == agent_id,
                    AgentProfile.workspace_id == scope.workspace_id,
                    self._visible(scope),
                )
                .with_for_update()
            )
            if agent is None:
                raise ApplicationError("AGENT_NOT_FOUND", "Agent 不存在", 404)
            if not self._can_manage(agent, scope, role):
                raise ApplicationError("AGENT_ACCESS_DENIED", "无权修改该 Agent", 403)
            if agent.version != body.expected_version:
                raise ApplicationError("AGENT_VERSION_CONFLICT", "Agent 已被其他人修改，请刷新后重试", 409)
            if body.visibility == "workspace" and role != "admin":
                raise ApplicationError("AGENT_ADMIN_REQUIRED", "只有空间管理员可以发布空间 Agent", 403)
            await self._validate_bindings(session, scope, body)
            agent.name = body.name
            agent.description = body.description
            agent.icon = body.icon
            agent.instructions = body.instructions
            agent.visibility = body.visibility
            agent.status = body.status
            agent.model = body.model or self.settings.deepseek_model
            agent.allow_planning = body.allow_planning
            agent.allow_delegation = body.allow_delegation
            agent.knowledge_scope = self._knowledge_scope(body)
            agent.starter_prompts = body.starter_prompts
            agent.version += 1
            agent.updated_at = datetime.now(UTC)
            await self._replace_capabilities(session, agent, body)
            await session.flush()
            return await self._view(session, agent, scope, role)

    async def archive_agent(self, scope: ResourceScope, agent_id: UUID) -> AgentView:
        """归档 Agent，保留运行快照和审计引用。

        Args:
            scope: 可信用户和空间。
            agent_id: Agent 标识。

        Returns:
            归档后的 Agent。
        """
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            agent = await session.scalar(
                select(AgentProfile)
                .where(
                    AgentProfile.id == agent_id,
                    AgentProfile.workspace_id == scope.workspace_id,
                    self._visible(scope),
                )
                .with_for_update()
            )
            if agent is None:
                raise ApplicationError("AGENT_NOT_FOUND", "Agent 不存在", 404)
            if not self._can_manage(agent, scope, role):
                raise ApplicationError("AGENT_ACCESS_DENIED", "无权归档该 Agent", 403)
            agent.status = "archived"
            agent.version += 1
            agent.updated_at = datetime.now(UTC)
            return await self._view(session, agent, scope, role)

    async def ensure_usable(
        self, session: AsyncSession, scope: ResourceScope, agent_id: UUID | None
    ) -> AgentProfile | None:
        """校验对话绑定的 Agent 可用于新运行；NULL 表示内置 Agent。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            agent_id: 对话绑定的 Agent。

        Returns:
            Agent 配置或 None。

        Raises:
            ApplicationError: 已归档、不可读或不属于当前空间。
        """
        if agent_id is None:
            return None
        agent = await session.scalar(
            select(AgentProfile).where(
                AgentProfile.id == agent_id,
                AgentProfile.workspace_id == scope.workspace_id,
                self._visible(scope),
            )
        )
        if agent is None:
            raise ApplicationError("AGENT_NOT_FOUND", "Agent 不存在", 404)
        if agent.status != "active":
            raise ApplicationError("AGENT_UNAVAILABLE", "Agent 已归档，不能开始新运行", 409)
        return agent

    async def ensure_usable_scope(self, scope: ResourceScope, agent_id: UUID | None) -> None:
        """在独立事务中校验 Agent 是否可用于创建对话。

        Args:
            scope: 可信用户和空间。
            agent_id: 目标 Agent；None 表示内置助手。
        """
        if agent_id is None:
            return
        async with self.factory() as session:
            await self._role(session, scope)
            await self.ensure_usable(session, scope, agent_id)

    async def switch_conversation_agent(
        self, scope: ResourceScope, conversation_id: UUID, agent_id: UUID | None
    ) -> ConversationView:
        """在没有活跃运行时切换对话的 Agent。

        Args:
            scope: 可信用户和空间。
            conversation_id: 对话标识。
            agent_id: 目标 Agent；None 表示内置助手。

        Returns:
            更新后的对话。

        Raises:
            ApplicationError: 对话不存在、正在生成或 Agent 不可用。
        """
        async with self.factory.begin() as session:
            await self._role(session, scope)
            conversation = await session.scalar(
                owned_conversations(scope).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)
            active = await session.scalar(
                select(Run.id).where(
                    Run.conversation_id == conversation_id,
                    Run.status.in_(("pending", "running")),
                )
            )
            if active is not None:
                raise ApplicationError("RUN_IN_PROGRESS", "当前对话正在生成，不能切换 Agent", 409)
            await self.ensure_usable(session, scope, agent_id)
            conversation.agent_profile_id = agent_id
            conversation.updated_at = datetime.now(UTC)
            await session.flush()
            return ConversationView.model_validate(conversation)

    @staticmethod
    def _knowledge_scope(body: AgentInput) -> dict[str, object]:
        """把请求中的知识库范围转换为持久化结构。

        Args:
            body: Agent 输入。

        Returns:
            知识库范围 JSON。
        """
        if body.knowledge_mode == "all_readable":
            return {"mode": "all_readable"}
        return {"mode": "selected", "kb_ids": [str(item) for item in body.knowledge_base_ids]}

    async def list_tools(self, scope: ResourceScope) -> list[ToolView]:
        """列出内置工具目录与当前空间启用状态。

        Args:
            scope: 可信用户和空间。

        Returns:
            工具目录。
        """
        async with self.factory() as session:
            await self._role(session, scope)
            policies = {
                key: enabled
                for key, enabled in (
                    await session.execute(
                        select(WorkspaceToolPolicy.tool_key, WorkspaceToolPolicy.enabled).where(
                            WorkspaceToolPolicy.workspace_id == scope.workspace_id
                        )
                    )
                ).all()
            }
            used = {
                key: count
                for key, count in (
                    await session.execute(
                        select(AgentCapability.capability_key, func.count())
                        .join(AgentProfile, AgentProfile.id == AgentCapability.agent_profile_id)
                        .where(
                            AgentProfile.workspace_id == scope.workspace_id,
                            AgentProfile.status == "active",
                            or_(
                                AgentProfile.visibility == "workspace",
                                AgentProfile.owner_user_id == scope.user_id,
                            ),
                            AgentCapability.kind == "tool",
                            AgentCapability.enabled.is_(True),
                        )
                        .group_by(AgentCapability.capability_key)
                    )
                ).all()
            }
        return [
            ToolView(
                key=spec.key,
                version=spec.version,
                display_name=spec.display_name,
                description=spec.description,
                risk=spec.risk,
                approval=spec.approval,
                timeout_seconds=spec.timeout_seconds,
                quota_per_run=spec.quota_per_run,
                input_schema=dict(spec.args_model.model_json_schema()),
                enabled=bool(policies.get(spec.key, True)),
                used_by_agents=int(used.get(spec.key, 0)),
            )
            for spec in self.registry.catalog()
        ]

    async def get_tool(self, scope: ResourceScope, key: str) -> ToolView:
        """读取单个工具目录项。

        Args:
            scope: 可信用户和空间。
            key: 工具标识。

        Returns:
            工具目录项。

        Raises:
            ApplicationError: 工具不存在。
        """
        for item in await self.list_tools(scope):
            if item.key == key:
                return item
        raise ApplicationError("TOOL_NOT_FOUND", "工具不存在", 404)

    async def set_tool_policy(self, scope: ResourceScope, key: str, enabled: bool) -> ToolView:
        """由空间管理员启停一个内置工具。

        Args:
            scope: 可信用户和空间。
            key: 工具标识。
            enabled: 是否启用。

        Returns:
            更新后的目录项。

        Raises:
            ApplicationError: 非管理员或工具不存在。
        """
        spec = self.registry.resolve(key)
        if spec is None or spec.system:
            raise ApplicationError("TOOL_NOT_FOUND", "工具不存在", 404)
        async with self.factory.begin() as session:
            role = await self._role(session, scope)
            if role != "admin":
                raise ApplicationError("TOOL_ADMIN_REQUIRED", "只有空间管理员可以启停工具", 403)
            row = await session.scalar(
                select(WorkspaceToolPolicy)
                .where(
                    WorkspaceToolPolicy.workspace_id == scope.workspace_id,
                    WorkspaceToolPolicy.tool_key == key,
                )
                .with_for_update()
            )
            if row is None:
                session.add(
                    WorkspaceToolPolicy(
                        workspace_id=scope.workspace_id,
                        tool_key=key,
                        enabled=enabled,
                        updated_by_user_id=scope.user_id,
                    )
                )
            else:
                row.enabled = enabled
                row.updated_by_user_id = scope.user_id
                row.updated_at = datetime.now(UTC)
        return await self.get_tool(scope, key)

    async def _validate_bindings(self, session: AsyncSession, scope: ResourceScope, body: AgentInput) -> None:
        """校验工具、技能和知识库均存在且不扩大权限。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            body: Agent 输入。

        Raises:
            ApplicationError: 绑定不合法或越权。
        """
        if len(body.tool_keys) > self.settings.agent_max_tools:
            raise ApplicationError("AGENT_TOOL_LIMIT", "绑定的工具数量超过上限", 422)
        if not self.settings.tools_enabled and body.tool_keys:
            raise ApplicationError("TOOLS_DISABLED", "当前环境未启用工具", 422)
        for key in body.tool_keys:
            spec = self.registry.resolve(key)
            if spec is None or spec.system:
                raise ApplicationError("TOOL_UNAVAILABLE", f"工具不可用：{key}", 422)
        if body.tool_keys:
            disabled = [
                key
                for key, enabled in (
                    await session.execute(
                        select(WorkspaceToolPolicy.tool_key, WorkspaceToolPolicy.enabled).where(
                            WorkspaceToolPolicy.workspace_id == scope.workspace_id,
                            WorkspaceToolPolicy.tool_key.in_(body.tool_keys),
                        )
                    )
                ).all()
                if not enabled
            ]
            if disabled:
                raise ApplicationError("TOOL_UNAVAILABLE", "工具已被空间管理员停用：" + "、".join(disabled), 422)
        if body.knowledge_mode == "selected" and not body.knowledge_base_ids:
            raise ApplicationError("KNOWLEDGE_SCOPE_EMPTY", "指定知识库范围时至少选择一个知识库", 422)
        await self._validate_knowledge(session, scope, body)
        await self._validate_skills(session, scope, body)

    async def _validate_knowledge(self, session: AsyncSession, scope: ResourceScope, body: AgentInput) -> None:
        """确认知识库可读，且空间 Agent 不绑定他人私有库。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            body: Agent 输入。

        Raises:
            ApplicationError: 知识库不存在或越权。
        """
        if body.knowledge_mode != "selected" or not body.knowledge_base_ids:
            return
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
        for _, visibility, owner_user_id in rows:
            if visibility == "private" and owner_user_id != scope.user_id:
                raise ApplicationError("KNOWLEDGE_ACCESS_DENIED", "无权使用该知识库", 403)
            if body.visibility == "workspace" and visibility != "workspace":
                raise ApplicationError("AGENT_KNOWLEDGE_SCOPE", "空间 Agent 只能绑定空间知识库", 422)

    async def _validate_skills(self, session: AsyncSession, scope: ResourceScope, body: AgentInput) -> None:
        """确认技能已发布、可读，且声明的工具已被 Agent 绑定。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            body: Agent 输入。

        Raises:
            ApplicationError: 技能不可用或越权。
        """
        if not body.skills:
            return
        ids = [item.id for item in body.skills]
        rows = list(
            await session.scalars(select(Skill).where(Skill.workspace_id == scope.workspace_id, Skill.id.in_(ids)))
        )
        if len(rows) != len(set(ids)):
            raise ApplicationError("SKILL_NOT_FOUND", "技能不存在", 404)
        tool_keys = set(body.tool_keys)
        for skill in rows:
            if skill.visibility == "private" and skill.owner_user_id != scope.user_id:
                raise ApplicationError("SKILL_ACCESS_DENIED", "无权使用该技能", 403)
            if body.visibility == "workspace" and skill.visibility != "workspace":
                raise ApplicationError("AGENT_SKILL_SCOPE", "空间 Agent 只能绑定空间技能", 422)
            version = await session.scalar(
                select(SkillVersion)
                .where(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.published_at.is_not(None),
                )
                .order_by(SkillVersion.version.desc())
                .limit(1)
            )
            if version is None:
                raise ApplicationError("SKILL_NOT_PUBLISHED", f"技能未发布：{skill.name}", 422)
            missing = set(version.tool_keys) - tool_keys
            if missing:
                raise ApplicationError(
                    "SKILL_TOOL_NOT_BOUND",
                    "技能声明的工具未在 Agent 中启用：" + "、".join(sorted(missing)),
                    422,
                )

    async def _replace_capabilities(self, session: AsyncSession, agent: AgentProfile, body: AgentInput) -> None:
        """原子替换 Agent 的工具与技能绑定。

        Args:
            session: 当前事务。
            agent: 已持久化 Agent。
            body: 新配置。
        """
        await session.execute(delete(AgentCapability).where(AgentCapability.agent_profile_id == agent.id))
        for key in body.tool_keys:
            session.add(AgentCapability(agent_profile_id=agent.id, kind="tool", capability_key=key))
        for item in body.skills:
            skill = await session.scalar(
                select(Skill).where(Skill.id == item.id, Skill.workspace_id == agent.workspace_id)
            )
            if skill is None:
                continue
            version = await session.scalar(
                select(SkillVersion)
                .where(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.published_at.is_not(None),
                )
                .order_by(SkillVersion.version.desc())
                .limit(1)
            )
            if version is None:
                continue
            session.add(
                AgentCapability(
                    agent_profile_id=agent.id,
                    kind="skill",
                    capability_key=str(skill.id),
                    skill_version_id=version.id,
                    activation=item.activation,
                )
            )

    async def _view(self, session: AsyncSession, agent: AgentProfile, scope: ResourceScope, role: str) -> AgentView:
        """组装公开视图，按权限决定是否返回指令正文。

        Args:
            session: 当前事务。
            agent: Agent 记录。
            scope: 当前用户和空间。
            role: 空间角色。

        Returns:
            Agent 视图。
        """
        can_manage = self._can_manage(agent, scope, role)
        rows = list(
            await session.scalars(
                select(AgentCapability).where(
                    AgentCapability.agent_profile_id == agent.id,
                    AgentCapability.enabled.is_(True),
                )
            )
        )
        policies = {
            key: enabled
            for key, enabled in (
                await session.execute(
                    select(WorkspaceToolPolicy.tool_key, WorkspaceToolPolicy.enabled).where(
                        WorkspaceToolPolicy.workspace_id == agent.workspace_id,
                        WorkspaceToolPolicy.tool_key.in_([row.capability_key for row in rows if row.kind == "tool"]),
                    )
                )
            ).all()
        }
        tools: list[ToolBindingView] = []
        for row in rows:
            if row.kind != "tool":
                continue
            spec = self.registry.resolve(row.capability_key)
            tools.append(
                ToolBindingView(
                    key=row.capability_key,
                    display_name=spec.display_name if spec else row.capability_key,
                    risk=spec.risk if spec else "read",
                    approval=spec.approval if spec else "never",
                    enabled=bool(policies.get(row.capability_key, True)) and spec is not None,
                )
            )
        skills: list[SkillBindingView] = []
        for row in rows:
            if row.kind != "skill" or row.skill_version_id is None:
                continue
            found = (
                await session.execute(
                    select(SkillVersion, Skill)
                    .join(Skill, SkillVersion.skill_id == Skill.id)
                    .where(SkillVersion.id == row.skill_version_id)
                )
            ).one_or_none()
            if found is None:
                continue
            version, skill = found
            skills.append(
                SkillBindingView(
                    id=skill.id,
                    key=skill.key,
                    name=skill.name,
                    version=version.version,
                    activation=row.activation,  # type: ignore[arg-type]
                )
            )
        return AgentView(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            icon=agent.icon,
            visibility=agent.visibility,  # type: ignore[arg-type]
            status=agent.status,  # type: ignore[arg-type]
            version=agent.version,
            model=agent.model,
            owner_user_id=agent.owner_user_id,
            is_owner=agent.owner_user_id == scope.user_id,
            can_manage=can_manage,
            instructions=agent.instructions if can_manage else None,
            tools=tools,
            skills=skills,
            knowledge_scope=agent.knowledge_scope,
            allow_planning=agent.allow_planning,
            allow_delegation=agent.allow_delegation,
            starter_prompts=agent.starter_prompts,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )
