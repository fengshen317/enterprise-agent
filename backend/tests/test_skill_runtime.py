"""技能装配、按需加载与上下文边界测试。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.capabilities.builtins.knowledge import knowledge_specs
from app.capabilities.builtins.skills import load_skill_spec
from app.capabilities.spec import AgentAssembly, SkillSnapshot
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.chat_agent import LoadSkillArgs
from app.schemas.knowledge import KnowledgeSearchArgs


def snapshot(key: str, content: str, activation: str) -> SkillSnapshot:
    """构造最小技能快照。

    Args:
        key: 技能标识。
        content: Markdown 正文。
        activation: always 或 on_demand。

    Returns:
        技能快照。
    """
    return SkillSnapshot(
        id=uuid4(),
        key=key,
        name=key,
        version=1,
        content=content,
        activation=activation,  # type: ignore[arg-type]
        tool_keys=(),
        knowledge_scope={"mode": "all_readable"},
        sha256="0" * 64,
    )


def assembly(settings: Settings) -> AgentAssembly:
    """构造带常驻与按需技能的装配。

    Args:
        settings: 测试配置。

    Returns:
        Agent 装配。
    """
    skills = (
        snapshot("policy", "ALWAYS-CONTENT", "always"),
        snapshot("manual", "DEMAND-CONTENT", "on_demand"),
    )
    return AgentAssembly(
        agent_id=None,
        name="测试助手",
        version=1,
        instructions="遵守流程。",
        model=settings.deepseek_model,
        tools=(),
        skills=skills,
        knowledge_scope={"mode": "all_readable"},
        allow_planning=True,
        allow_delegation=True,
        snapshot={},
    )


def test_prompt_injects_always_on_and_hides_on_demand() -> None:
    """常驻技能进入提示词，按需技能只显示目录。"""
    settings = Settings(_env_file=None, environment="test")
    prompt = assembly(settings).system_prompt()
    assert "ALWAYS-CONTENT" in prompt
    assert "DEMAND-CONTENT" not in prompt
    assert "load_skill" in prompt
    assert "policy" in prompt and "manual" in prompt


def test_child_assembly_has_no_skills_or_system_tools() -> None:
    """子 Agent 不能加载技能，也不能获得系统工具。"""
    settings = Settings(_env_file=None, environment="test")
    child = assembly(settings).for_child()
    assert child.skills == ()
    assert child.tools == ()
    assert child.allow_planning is False and child.allow_delegation is False
    assert "ALWAYS-CONTENT" not in child.system_prompt("analyst")


def test_load_skill_returns_content_without_audit_leak() -> None:
    """加载工具返回正文，但审计只保存标识与长度。"""
    settings = Settings(_env_file=None, environment="test", skill_load_max_characters=500)
    spec = load_skill_spec(settings)
    skill = snapshot("manual", "x" * 600, "on_demand")
    context = SimpleNamespace(settings=settings, skills={"manual": skill})
    result = asyncio.run(spec.handler(context, LoadSkillArgs(key="manual")))  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert len(str(result["content"])) == 500 and result["truncated"] is True
    audit = spec.audit(LoadSkillArgs(key="manual"), result)
    assert audit == {"key": "manual", "version": 1, "length": 500}
    assert "x" * 20 not in str(audit)


def test_load_skill_rejects_unbound_key() -> None:
    """未绑定的技能不能被模型加载。"""
    settings = Settings(_env_file=None, environment="test")
    spec = load_skill_spec(settings)
    context = SimpleNamespace(settings=settings, skills={})
    with pytest.raises(ApplicationError, match="未绑定"):
        asyncio.run(spec.handler(context, LoadSkillArgs(key="missing")))  # type: ignore[arg-type]


def test_selected_empty_knowledge_scope_cannot_fall_back_to_all() -> None:
    """指定知识库全部失效时，检索必须拒绝而不是回退到全部可读库。"""
    settings = Settings(_env_file=None, environment="test")
    spec = knowledge_specs(settings)[0]
    assembly = AgentAssembly(
        agent_id=None,
        name="测试助手",
        version=1,
        instructions="",
        model=settings.deepseek_model,
        tools=(),
        skills=(),
        knowledge_scope={"mode": "selected", "kb_ids": []},
        allow_planning=True,
        allow_delegation=True,
        snapshot={},
    )
    context = SimpleNamespace(assembly=assembly, executor=SimpleNamespace())
    with pytest.raises(ApplicationError, match="没有可用的知识库"):
        asyncio.run(spec.handler(context, KnowledgeSearchArgs(query="制度")))  # type: ignore[arg-type]
