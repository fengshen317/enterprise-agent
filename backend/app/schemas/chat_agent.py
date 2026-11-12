"""对话规划和委派的有界输入，身份始终由服务端绑定。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PLAN_TOOL = "update_plan"
DELEGATE_TOOL = "delegate_tasks"
CHILD_TOOL = "delegate_agent"


class PlanItem(BaseModel):
    """公开工作步骤，不保存模型内部推理。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class PlanArgs(BaseModel):
    """每次更新提交完整计划，便于刷新重建。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    steps: list[PlanItem] = Field(min_length=1, max_length=8)


class DelegatedTask(BaseModel):
    """子 Agent 只能获得主 Agent 明确交付的上下文。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["analyst", "reviewer"]
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=12000)


class DelegateArgs(BaseModel):
    """同批任务必须相互独立，依赖结果由主 Agent 下一轮传递。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    tasks: list[DelegatedTask] = Field(min_length=1, max_length=2)


class LoadSkillArgs(BaseModel):
    """按需加载技能正文，只能使用当前 Agent 已绑定的技能。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=64)
