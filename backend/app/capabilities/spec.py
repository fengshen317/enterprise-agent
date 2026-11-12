"""能力注册的稳定契约：工具、技能与运行装配结果。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel

from app.capabilities.prompts import BASE_SYSTEM_PROMPT, CHILD_SYSTEM_PROMPT, SAFETY_RULES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import Settings
    from app.schemas.auth import ResourceScope
    from app.services.knowledge import KnowledgeService
    from app.services.schedule_store import ScheduleStore
    from app.services.tools import ToolExecutor

RiskLevel = Literal["read", "write", "delete", "external"]
ApprovalMode = Literal["never", "always"]
ActivationMode = Literal["always", "on_demand"]
ToolHandler = Callable[["ToolContext", BaseModel], Awaitable[object]]
AuditBuilder = Callable[[BaseModel, object], dict[str, object]]
ApprovalPreview = Callable[[BaseModel, "ToolContext"], dict[str, object]]


@dataclass(frozen=True)
class SkillSnapshot:
    """运行装配时固定下来的技能版本，正文不进入审计。"""

    id: UUID
    key: str
    name: str
    version: int
    content: str
    activation: ActivationMode
    tool_keys: tuple[str, ...]
    knowledge_scope: dict[str, object]
    sha256: str


@dataclass(frozen=True)
class ToolContext:
    """单次工具调用的可信上下文，身份与依赖均来自服务端。"""

    executor: ToolExecutor
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    run_id: UUID
    conversation_id: UUID
    scope: ResourceScope
    knowledge_service: KnowledgeService | None = None
    schedule_store: ScheduleStore | None = None
    assembly: AgentAssembly | None = None
    skills: Mapping[str, SkillSnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """一个可注册工具的完整声明；handler 不出现在模型可见协议中。"""

    key: str
    version: int
    display_name: str
    description: str
    args_model: type[BaseModel]
    risk: RiskLevel
    approval: ApprovalMode
    timeout_seconds: float
    quota_per_run: int
    handler: ToolHandler
    audit: AuditBuilder
    approval_preview: ApprovalPreview | None = None
    child_allowed: bool = True
    context_eligible: bool = False
    system: bool = False
    source: Literal["builtin"] = "builtin"

    def __post_init__(self) -> None:
        """拒绝会在执行层产生歧义的能力声明。

        Raises:
            ValueError: 标识、风险/审批组合或预算不合法。
        """
        if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.key) is None:
            raise ValueError(f"非法工具标识: {self.key}")
        if self.version < 1:
            raise ValueError("工具版本必须大于 0")
        if self.timeout_seconds <= 0 or self.quota_per_run < 1:
            raise ValueError("工具超时与配额必须为正数")
        if self.risk != "read" and self.approval != "always":
            raise ValueError("写入/删除/外发工具必须逐次审批")
        if self.risk != "read" and self.child_allowed:
            raise ValueError("子 Agent 只能使用只读工具")


@dataclass(frozen=True)
class AgentAssembly:
    """一次运行的有效能力快照；运行中不再读取可变配置。"""

    agent_id: UUID | None
    name: str
    version: int
    instructions: str
    model: str
    tools: tuple[ToolSpec, ...]
    skills: tuple[SkillSnapshot, ...]
    knowledge_scope: dict[str, object]
    allow_planning: bool
    allow_delegation: bool
    snapshot: dict[str, object] = field(default_factory=dict)

    @property
    def tool_keys(self) -> frozenset[str]:
        """返回当前允许模型调用的工具标识。

        Returns:
            工具标识集合。
        """
        return frozenset(item.key for item in self.tools)

    @property
    def tool_map(self) -> dict[str, ToolSpec]:
        """按标识索引工具，执行层不依赖顺序。

        Returns:
            工具标识到声明的映射。
        """
        return {item.key: item for item in self.tools}

    def selected_kb_ids(self) -> set[UUID] | None:
        """读取 Agent 的知识库范围；None 表示全部可读。

        Returns:
            选定的应用知识库 ID 集合，或 None。
        """
        if self.knowledge_scope.get("mode") != "selected":
            return None
        raw = self.knowledge_scope.get("kb_ids", [])
        if not isinstance(raw, list):
            return set()
        result: set[UUID] = set()
        for item in raw:
            try:
                result.add(UUID(str(item)))
            except ValueError:
                continue
        return result

    def auto_retrieve_enabled(self) -> bool:
        """读取自动检索开关；旧快照缺省为开启。

        Returns:
            是否启用自动知识库检索。
        """
        value = self.knowledge_scope.get("auto_retrieve")
        return value is not False

    def for_child(self) -> AgentAssembly:
        """构造只读收窄的子 Agent 装配，禁止写操作与再委派。

        Returns:
            子 Agent 能力快照。
        """
        tools = tuple(item for item in self.tools if item.risk == "read" and item.child_allowed and not item.system)
        return AgentAssembly(
            agent_id=self.agent_id,
            name=self.name,
            version=self.version,
            instructions="",
            model=self.model,
            tools=tools,
            skills=(),
            knowledge_scope=self.knowledge_scope,
            allow_planning=False,
            allow_delegation=False,
            snapshot={
                **self.snapshot,
                "child": True,
                "tools": [{"key": item.key, "version": item.version} for item in tools],
                "skills": [],
            },
        )

    def system_prompt(self, child_role: str | None = None) -> str:
        """生成模型可见的职责、技能与能力目录。

        Args:
            child_role: 子 Agent 角色；为空时生成主 Agent 提示。

        Returns:
            完整 system prompt。
        """
        if child_role is not None:
            return self._child_prompt(child_role)
        parts = [BASE_SYSTEM_PROMPT]
        if self.instructions.strip():
            parts.append("## Agent 指令\n" + self.instructions.strip())
        always = [skill for skill in self.skills if skill.activation == "always"]
        if always:
            parts.append("## 已启用技能\n" + "\n\n".join(self._skill_block(skill) for skill in always))
        demand = [skill for skill in self.skills if skill.activation == "on_demand"]
        if demand:
            catalog = "\n".join(f"- {skill.key}：{skill.name}（需要时用 load_skill 加载）" for skill in demand)
            parts.append("## 可按需加载的技能\n" + catalog)
        parts.append("## 可用工具\n" + self._tool_catalog(self.tools))
        parts.append(SAFETY_RULES)
        return "\n\n".join(parts)

    def _child_prompt(self, child_role: str) -> str:
        """子 Agent 只看到只读工具，不能修改计划或继续委派。

        Args:
            child_role: analyst 或 reviewer。

        Returns:
            子 Agent system prompt。
        """
        label = "分析员" if child_role == "analyst" else "复核员"
        read_tools = tuple(item for item in self.tools if item.risk == "read" and not item.system)
        return "\n\n".join(
            [
                CHILD_SYSTEM_PROMPT.format(role=label),
                "## 可用只读工具\n" + self._tool_catalog(read_tools),
                SAFETY_RULES,
            ]
        )

    @staticmethod
    def _tool_catalog(tools: tuple[ToolSpec, ...]) -> str:
        """列出工具用途与风险，让模型知道何时调用和是否需要审批。

        Args:
            tools: 当前装配允许的工具。

        Returns:
            面向模型的紧凑目录。
        """
        if not tools:
            return "本次没有可用工具。"
        labels = {
            "read": "只读",
            "write": "写入，需用户审批",
            "delete": "删除，需用户审批",
            "external": "外发，需用户审批",
        }
        return "\n".join(f"- {item.key}：{item.description}（{labels[item.risk]}）" for item in tools)

    @staticmethod
    def _skill_block(skill: SkillSnapshot) -> str:
        """用明确边界包住技能正文，避免与系统规则混淆。

        Args:
            skill: 已固定版本的技能。

        Returns:
            可注入提示词的技能段。
        """
        return f'<skill key="{skill.key}" version="{skill.version}">\n{skill.content}\n</skill>'
