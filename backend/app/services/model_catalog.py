"""问答模型白名单、输出上限和推理模型判断。"""

from app.core.config import Settings
from app.core.errors import ApplicationError


def normalize_model_name(value: str | None) -> str | None:
    """把空白模型名视为未选择。

    Args:
        value: 配置、对话或 Agent 上的原始模型名。

    Returns:
        去掉首尾空白后的名称；空白返回 None。
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def resolve_model(settings: Settings, conversation_model: str | None, agent_model: str | None) -> str:
    """按对话、Agent、全局默认的顺序决定本次实际模型。

    对话上的显式选择必须落在白名单内。Agent 档案中的名称不在白名单时继续回退，
    避免一份过期档案阻断全部问答。

    Args:
        settings: 当前进程配置。
        conversation_model: 对话级选择，空表示未指定。
        agent_model: Agent 档案中的模型，空表示未指定。

    Returns:
        本次运行应调用的模型名。

    Raises:
        ApplicationError: 显式选择或最终默认模型不在白名单中。
    """
    explicit = normalize_model_name(conversation_model)
    if explicit is not None:
        if explicit not in settings.allowed_models:
            raise ApplicationError("MODEL_NOT_ALLOWED", "当前对话选择的模型不在允许列表中", 422)
        return explicit
    agent = normalize_model_name(agent_model)
    if agent is not None and agent in settings.allowed_models:
        return agent
    default = normalize_model_name(settings.deepseek_model) or ""
    if default not in settings.allowed_models:
        raise ApplicationError("MODEL_NOT_ALLOWED", "默认模型不在允许列表中", 422)
    return default


def is_reasoner(settings: Settings, model: str) -> bool:
    """判断模型是否应打开思考过程。

    Args:
        settings: 当前进程配置。
        model: 已解析的模型名。

    Returns:
        是否属于推理模型。
    """
    return model in settings.reasoner_models


def max_tokens_for(settings: Settings, model: str) -> int:
    """返回该模型的输出 token 上限。

    Args:
        settings: 当前进程配置。
        model: 已解析的模型名。

    Returns:
        传给供应商的 max_tokens。未单列的模型使用全局默认值。
    """
    return int(settings.model_max_tokens_by_model.get(model, settings.model_max_tokens))


def summary_model_for(settings: Settings, run_model: str) -> str:
    """摘要使用非推理模型，避免把思考过程写进可复用摘要。

    Args:
        settings: 当前进程配置。
        run_model: 本轮回答使用的模型。

    Returns:
        摘要调用的模型名。没有可用的非推理模型时才退回本轮模型。
    """
    if not is_reasoner(settings, run_model):
        return run_model
    for candidate in (settings.deepseek_model, *settings.allowed_models):
        name = normalize_model_name(candidate)
        if name is not None and name in settings.allowed_models and not is_reasoner(settings, name):
            return name
    return run_model


def thinking_extra(settings: Settings, model: str) -> dict[str, object]:
    """构造 DeepSeek 思考开关。摘要调用不使用此函数。

    Args:
        settings: 当前进程配置。
        model: 已解析的模型名。

    Returns:
        传给 ChatDeepSeek 的 extra_body。
    """
    mode = "enabled" if is_reasoner(settings, model) else "disabled"
    return {"thinking": {"type": mode}}
