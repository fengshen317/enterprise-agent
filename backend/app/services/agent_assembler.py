"""把 Agent 配置、工具目录和技能版本装配成一次运行的能力快照。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capabilities.registry import ToolRegistry
from app.capabilities.spec import AgentAssembly, SkillSnapshot, ToolSpec
from app.core.errors import ApplicationError
from app.models.agent import AgentCapability, AgentProfile, WorkspaceToolPolicy
from app.models.conversation import Conversation, Run
from app.models.knowledge import KnowledgeBase
from app.models.skill import Skill, SkillVersion
from app.schemas.auth import ResourceScope

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.services.knowledge import KnowledgeService


class AgentAssembler:
    """从数据库配置生成运行快照；快照在运行开始时固定。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        registry: ToolRegistry,
        settings: Settings,
        knowledge_service: KnowledgeService | None = None,
    ) -> None:
        """保存会话工厂、工具目录与预算。

        Args:
            factory: 独立数据库会话工厂。
            registry: 内置工具目录。
            settings: 运行预算与模型配置。
            knowledge_service: 可选知识库服务。
        """
        self.factory = factory
        self.registry = registry
        self.settings = settings
        self.knowledge_service = knowledge_service

    def default_assembly(self, name: str = "序列助手", instructions: str = "") -> AgentAssembly:
        """构造与当前产品能力一致的内置 Agent，保证旧对话行为不变。

        Args:
            name: 内置 Agent 名称。
            instructions: 可选附加指令。

        Returns:
            运行能力快照。
        """
        # 内置助手保持最小权限：写工具必须由用户创建/选择 Agent 后显式绑定。
        tools = tuple(item for item in self.registry.catalog() if item.risk == "read")
        digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
        snapshot = self._snapshot(
            agent_id=None,
            name=name,
            version=0,
            instructions=instructions,
            tools=tools,
            skills=(),
            knowledge_scope={"mode": "all_readable"},
            allow_planning=True,
            allow_delegation=True,
        )
        snapshot["instructions_sha256"] = digest
        return AgentAssembly(
            agent_id=None,
            name=name,
            version=0,
            instructions=instructions,
            model=self.settings.deepseek_model,
            tools=tools,
            skills=(),
            knowledge_scope={"mode": "all_readable"},
            allow_planning=True,
            allow_delegation=True,
            snapshot=snapshot,
        )

    async def assemble(self, scope: ResourceScope, agent_profile_id: UUID | None) -> AgentAssembly:
        """按 Agent 配置装配工具、技能和知识库范围。

        Args:
            scope: 可信用户和空间。
            agent_profile_id: 对话绑定的 Agent；None 使用内置 Agent。

        Returns:
            当前运行能力。

        Raises:
            ApplicationError: Agent 不存在、不可读或已归档。
        """
        if agent_profile_id is None:
            return self.default_assembly()
        async with self.factory() as session:
            agent = await self._readable_agent(session, scope, agent_profile_id)
            if agent.status != "active":
                raise ApplicationError("AGENT_UNAVAILABLE", "Agent 已归档，不能开始新运行", 409)
            return await self._assemble_in_session(session, scope, agent)

    async def assemble_for_run(self, run_id: UUID) -> AgentAssembly:
        """优先按运行快照恢复，缺失时按对话当前 Agent 装配。

        Args:
            run_id: 已领取的运行标识。

        Returns:
            与提交时刻一致的能力快照。

        Raises:
            ApplicationError: 运行或身份不可用。
        """
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(
                        Conversation.created_by_user_id,
                        Conversation.workspace_id,
                        Conversation.agent_profile_id,
                        Run.agent_snapshot,
                    )
                    .join(Run, Run.conversation_id == Conversation.id)
                    .where(Run.id == run_id, Conversation.status == "active")
                )
            ).one_or_none()
            if row is None or row[0] is None or row[1] is None:
                raise ApplicationError("ACCESS_REVOKED", "当前运行已结束或无执行权限", 403)
            scope = ResourceScope(row[0], row[1])
            snapshot = row[3] if isinstance(row[3], dict) else {}
            if snapshot.get("instructions") is not None and snapshot.get("agent_name"):
                return await self._restore(session, scope, snapshot)
            return await self.assemble(scope, row[2])

    async def snapshot_for_conversation(
        self, scope: ResourceScope, conversation_id: UUID
    ) -> tuple[dict[str, object], UUID | None, str | None, str]:
        """在创建运行前生成能力快照，保证提交和执行使用同一配置。

        Args:
            scope: 可信用户和空间。
            conversation_id: 目标对话。

        Returns:
            (快照, Agent 标识, 对话级模型, Agent 或默认模型)。

        Raises:
            ApplicationError: 对话不存在。
        """
        async with self.factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.workspace_id == scope.workspace_id,
                    Conversation.created_by_user_id == scope.user_id,
                )
            )
            if conversation is None:
                raise ApplicationError("CONVERSATION_NOT_FOUND", "对话不存在", 404)
            assembly = await self.assemble(scope, conversation.agent_profile_id)
            conversation_model = conversation.model
        return assembly.snapshot, assembly.agent_id, conversation_model, assembly.model

    async def _readable_agent(
        self, session: AsyncSession, scope: ResourceScope, agent_profile_id: UUID
    ) -> AgentProfile:
        """读取当前空间内可访问的 Agent。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            agent_profile_id: Agent 标识。

        Returns:
            Agent 记录。

        Raises:
            ApplicationError: 不存在或不可读。
        """
        agent = await session.scalar(
            select(AgentProfile).where(
                AgentProfile.id == agent_profile_id,
                AgentProfile.workspace_id == scope.workspace_id,
                or_(
                    AgentProfile.visibility == "workspace",
                    AgentProfile.owner_user_id == scope.user_id,
                ),
            )
        )
        if agent is None:
            raise ApplicationError("AGENT_NOT_FOUND", "Agent 不存在", 404)
        return agent

    async def _assemble_in_session(
        self, session: AsyncSession, scope: ResourceScope, agent: AgentProfile
    ) -> AgentAssembly:
        """在已有会话中完成工具、技能和知识库裁剪。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            agent: Agent 配置。

        Returns:
            运行能力。
        """
        capabilities = list(
            await session.scalars(
                select(AgentCapability).where(
                    AgentCapability.agent_profile_id == agent.id,
                    AgentCapability.enabled.is_(True),
                )
            )
        )
        policies = await self._policies(session, agent.workspace_id)
        tool_map: dict[str, ToolSpec] = {}
        for row in capabilities:
            if row.kind != "tool":
                continue
            spec = self.registry.resolve(row.capability_key)
            if spec is not None and not spec.system and policies.get(spec.key, True):
                tool_map[spec.key] = spec
        tools = tuple(tool_map.values())
        skills = await self._skills(session, capabilities, set(tool_map))
        knowledge_scope = await self._knowledge_scope(session, scope, agent)
        snapshot = self._snapshot(
            agent_id=agent.id,
            name=agent.name,
            version=agent.version,
            instructions=agent.instructions,
            tools=tools,
            skills=skills,
            knowledge_scope=knowledge_scope,
            allow_planning=agent.allow_planning,
            allow_delegation=agent.allow_delegation,
        )
        return AgentAssembly(
            agent_id=agent.id,
            name=agent.name,
            version=agent.version,
            instructions=agent.instructions,
            model=agent.model or self.settings.deepseek_model,
            tools=tools,
            skills=skills,
            knowledge_scope=knowledge_scope,
            allow_planning=agent.allow_planning,
            allow_delegation=agent.allow_delegation,
            snapshot=snapshot,
        )

    async def _skills(
        self,
        session: AsyncSession,
        capabilities: list[AgentCapability],
        tool_keys: set[str],
    ) -> tuple[SkillSnapshot, ...]:
        """加载固定版本技能，并裁剪上下文预算与不可用工具声明。

        Args:
            session: 当前事务。
            capabilities: Agent 能力绑定。
            tool_keys: 当前 Agent 工具集合。

        Returns:
            已固定版本的技能。
        """
        result: list[SkillSnapshot] = []
        always_chars = 0
        always_count = 0
        for row in capabilities:
            if row.kind != "skill" or row.skill_version_id is None:
                continue
            found = (
                await session.execute(
                    select(SkillVersion, Skill)
                    .join(Skill, SkillVersion.skill_id == Skill.id)
                    .where(SkillVersion.id == row.skill_version_id, Skill.status != "archived")
                )
            ).one_or_none()
            if found is None:
                continue
            version, skill = found
            if set(version.tool_keys) - tool_keys:
                continue
            activation = row.activation
            if activation == "always":
                if always_count >= self.settings.skill_always_on_max:
                    continue
                if always_chars + len(version.content_md) > self.settings.skill_context_characters:
                    continue
                always_count += 1
                always_chars += len(version.content_md)
            result.append(
                SkillSnapshot(
                    id=skill.id,
                    key=skill.key,
                    name=skill.name,
                    version=version.version,
                    content=version.content_md,
                    activation=activation,  # type: ignore[arg-type]
                    tool_keys=tuple(version.tool_keys),
                    knowledge_scope=version.knowledge_scope,
                    sha256=version.content_sha256,
                )
            )
        return tuple(result)

    async def _knowledge_scope(
        self, session: AsyncSession, scope: ResourceScope, agent: AgentProfile
    ) -> dict[str, object]:
        """把 Agent 声明的知识库范围裁剪到当前用户可读集合。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            agent: Agent 配置。

        Returns:
            可持久化的知识库范围。
        """
        raw = agent.knowledge_scope if isinstance(agent.knowledge_scope, dict) else {}
        if raw.get("mode") != "selected":
            return {"mode": "all_readable"}
        raw_ids = raw.get("kb_ids")
        if not isinstance(raw_ids, list):
            return {"mode": "selected", "kb_ids": []}
        requested = [str(item) for item in raw_ids if isinstance(item, (str, int))]
        if not requested:
            return {"mode": "selected", "kb_ids": []}
        rows = (
            await session.execute(
                select(KnowledgeBase.id, KnowledgeBase.visibility, KnowledgeBase.owner_user_id).where(
                    KnowledgeBase.workspace_id == scope.workspace_id,
                    KnowledgeBase.id.in_(requested),
                )
            )
        ).all()
        allowed = [
            str(kb_id)
            for kb_id, visibility, owner_user_id in rows
            if visibility == "workspace" or owner_user_id == scope.user_id
            if agent.visibility == "workspace" and visibility == "workspace" or agent.visibility != "workspace"
        ]
        return {"mode": "selected", "kb_ids": allowed}

    async def _policies(self, session: AsyncSession, workspace_id: UUID) -> dict[str, bool]:
        """读取空间工具启停策略。

        Args:
            session: 当前事务。
            workspace_id: 空间标识。

        Returns:
            工具标识到启用状态的映射。
        """
        return {
            key: enabled
            for key, enabled in (
                await session.execute(
                    select(WorkspaceToolPolicy.tool_key, WorkspaceToolPolicy.enabled).where(
                        WorkspaceToolPolicy.workspace_id == workspace_id
                    )
                )
            ).all()
        }

    def _snapshot(
        self,
        *,
        agent_id: UUID | None,
        name: str,
        version: int,
        instructions: str,
        tools: tuple[ToolSpec, ...],
        skills: tuple[SkillSnapshot, ...],
        knowledge_scope: dict[str, object],
        allow_planning: bool,
        allow_delegation: bool,
    ) -> dict[str, object]:
        """生成不可变运行快照；正文只保留指令，技能只保留版本标识。

        Args:
            agent_id: Agent 标识。
            name: 名称。
            version: 配置版本。
            instructions: 指令正文。
            tools: 工具集合。
            skills: 技能集合。
            knowledge_scope: 知识库范围。
            allow_planning: 是否允许规划。
            allow_delegation: 是否允许委派。

        Returns:
            可落库 JSON。
        """
        return {
            "schema": 1,
            "agent_profile_id": str(agent_id) if agent_id is not None else None,
            "agent_name": name,
            "agent_version": version,
            "instructions": instructions,
            "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "model": self.settings.deepseek_model,
            "tools": [{"key": item.key, "version": item.version} for item in tools],
            "skills": [
                {
                    "id": str(item.id),
                    "key": item.key,
                    "version": item.version,
                    "activation": item.activation,
                    "sha256": item.sha256,
                }
                for item in skills
            ],
            "knowledge_scope": knowledge_scope,
            "policy": {
                "write": "approval",
                "approval_timeout_seconds": self.settings.approval_timeout_seconds,
            },
            "allow_planning": allow_planning,
            "allow_delegation": allow_delegation,
        }

    async def _restore(self, session: AsyncSession, scope: ResourceScope, snapshot: dict[str, object]) -> AgentAssembly:
        """从运行快照恢复能力，权限策略仍按当前状态复核。

        Args:
            session: 当前事务。
            scope: 可信用户和空间。
            snapshot: 已持久化快照。

        Returns:
            可执行能力。
        """
        policies = await self._policies(session, scope.workspace_id)
        tools: list[ToolSpec] = []
        raw_tools = snapshot.get("tools")
        if isinstance(raw_tools, list):
            for item in raw_tools:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                if not isinstance(key, str):
                    continue
                spec = self.registry.resolve(key)
                if spec is not None and not spec.system and policies.get(key, True):
                    tools.append(spec)
        skills: list[SkillSnapshot] = []
        tool_keys = {item.key for item in tools}
        raw_skills = snapshot.get("skills")
        if isinstance(raw_skills, list):
            for item in raw_skills:
                if not isinstance(item, dict):
                    continue
                skill_id = item.get("id")
                version_number = item.get("version")
                if not isinstance(skill_id, str) or not isinstance(version_number, int):
                    continue
                found = (
                    await session.execute(
                        select(SkillVersion, Skill)
                        .join(Skill, SkillVersion.skill_id == Skill.id)
                        .where(
                            Skill.id == skill_id,
                            SkillVersion.version == version_number,
                            Skill.status != "archived",
                        )
                    )
                ).one_or_none()
                if found is None:
                    continue
                version, skill = found
                if set(version.tool_keys) - tool_keys:
                    continue
                activation = item.get("activation")
                skills.append(
                    SkillSnapshot(
                        id=skill.id,
                        key=skill.key,
                        name=skill.name,
                        version=version.version,
                        content=version.content_md,
                        activation=activation if activation in {"always", "on_demand"} else "on_demand",
                        tool_keys=tuple(version.tool_keys),
                        knowledge_scope=version.knowledge_scope,
                        sha256=version.content_sha256,
                    )
                )
        instructions = str(snapshot.get("instructions", ""))
        raw_scope = snapshot.get("knowledge_scope")
        knowledge_scope: dict[str, object] = (
            cast(dict[str, object], raw_scope) if isinstance(raw_scope, dict) else {"mode": "all_readable"}
        )
        raw_agent_id = snapshot.get("agent_profile_id")
        agent_id = UUID(raw_agent_id) if isinstance(raw_agent_id, str) else None
        raw_version = snapshot.get("agent_version")
        version_number = raw_version if isinstance(raw_version, int) else 0
        return AgentAssembly(
            agent_id=agent_id,
            name=str(snapshot.get("agent_name", "序列助手")),
            version=version_number,
            instructions=instructions,
            model=str(snapshot.get("model", self.settings.deepseek_model)),
            tools=tuple(tools),
            skills=tuple(skills),
            knowledge_scope=knowledge_scope,
            allow_planning=bool(snapshot.get("allow_planning", True)),
            allow_delegation=bool(snapshot.get("allow_delegation", True)),
            snapshot=snapshot,
        )
