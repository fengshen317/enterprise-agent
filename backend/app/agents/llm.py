"""统一构造问答、摘要、子 Agent 与计划使用的 LLM。"""

from typing import TYPE_CHECKING

from httpx import Timeout
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ApplicationError

if TYPE_CHECKING:
    from langchain_deepseek import ChatDeepSeek
    from langchain_openai import ChatOpenAI


def vision_model(settings: Settings) -> str:
    """确定有图片时应记录并调用的模型。"""
    if settings.llm_provider == "openai_compatible":
        return settings.llm_vision_model or settings.llm_model
    from app.services.chat_attachments import CHAT_VISION_MODEL

    return CHAT_VISION_MODEL


def validate_llm(settings: Settings) -> None:
    """确认当前供应商所需参数齐备。"""
    configured = (
        settings.deepseek_api_key.get_secret_value().strip()
        if settings.llm_provider == "deepseek"
        else settings.llm_model.strip()
    )
    if not configured:
        raise ApplicationError("MODEL_NOT_CONFIGURED", "请由系统管理员在「API 配置」中设置语言模型", 503)


def create_llm(
    settings: Settings, *, streaming: bool = False, max_tokens: int | None = None, vision: bool = False
) -> "ChatOpenAI | ChatDeepSeek":
    """按供应商构造单次运行的客户端，避免在运行中混用凭据。"""
    validate_llm(settings)
    if settings.llm_provider == "openai_compatible":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=vision_model(settings) if vision else settings.llm_model,
            api_key=settings.llm_api_key if settings.llm_api_key.get_secret_value() else SecretStr("local-no-auth"),
            base_url=settings.llm_base_url,
            # SDK 会缓存数字超时对应的客户端，独立超时对象避免结束一轮时关闭其他轮次的连接。
            timeout=Timeout(settings.model_timeout_seconds),
            max_completion_tokens=max_tokens or settings.model_max_tokens,
            max_retries=0,
            streaming=streaming,
            use_responses_api=False,
        )

    from langchain_deepseek import ChatDeepSeek

    return ChatDeepSeek(
        model=vision_model(settings) if vision else settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url="https://api.deepseek.com",
        timeout=settings.model_timeout_seconds,
        max_tokens=max_tokens or settings.model_max_tokens,
        max_retries=0,
        streaming=streaming,
        extra_body={"thinking": {"type": "disabled"}},
    )
