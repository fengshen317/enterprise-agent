"""内置工具注册表；工具目录来自代码，启用策略由装配层裁剪。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.capabilities.builtins.knowledge import knowledge_specs
from app.capabilities.builtins.run_stats import run_stats_spec
from app.capabilities.builtins.scheduled import scheduled_spec
from app.capabilities.builtins.web_search import web_search_spec
from app.capabilities.spec import ToolSpec

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.services.knowledge import KnowledgeService
    from app.services.schedule_store import ScheduleStore


class ToolRegistry:
    """按稳定标识注册工具，拒绝重复和空目录歧义。"""

    def __init__(self) -> None:
        """创建空注册表。"""
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册单个工具声明。

        Args:
            spec: 已通过自校验的工具声明。

        Raises:
            ValueError: 同一标识重复注册。
        """
        if spec.key in self._specs:
            raise ValueError(f"工具重复注册: {spec.key}")
        self._specs[spec.key] = spec

    def resolve(self, key: str) -> ToolSpec | None:
        """按标识查找工具。

        Args:
            key: 稳定工具标识。

        Returns:
            工具声明或空。
        """
        return self._specs.get(key)

    def catalog(self) -> tuple[ToolSpec, ...]:
        """返回可绑定给 Agent 的业务工具，不包含系统工具。

        Returns:
            按注册顺序排列的工具。
        """
        return tuple(spec for spec in self._specs.values() if not spec.system)

    def all(self) -> tuple[ToolSpec, ...]:
        """返回全部工具，供装配层追加系统工具时判断冲突。

        Returns:
            全部工具声明。
        """
        return tuple(self._specs.values())


def build_default_registry(
    settings: Settings,
    knowledge_service: KnowledgeService | None = None,
    schedule_store: ScheduleStore | None = None,
) -> ToolRegistry:
    """构造当前部署可用的内置工具目录。

    Args:
        settings: 开关与预算配置。
        knowledge_service: 知识库服务；未启用时只注册运行统计。
        schedule_store: 定时任务存储；未启用时不注册写工具。

    Returns:
        工具注册表。
    """
    registry = ToolRegistry()
    registry.register(run_stats_spec(settings))
    if knowledge_service is not None:
        for spec in knowledge_specs(settings):
            registry.register(spec)
    if schedule_store is not None and settings.schedule_enabled:
        registry.register(scheduled_spec(settings))
    if settings.tavily_api_key.get_secret_value().strip():
        registry.register(web_search_spec(settings))
    return registry
