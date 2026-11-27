"""用临时配置验证语言模型和搜索服务，只返回固定的诊断提示。"""

import asyncio
from typing import Literal

import httpx
from langchain_core.messages import HumanMessage
from langsmith import tracing_context
from pydantic import BaseModel

from app.agents.llm import create_llm
from app.capabilities.builtins.web_search import search_tavily
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.services.config_service import ConfigGroup
from app.services.task_retry import classify_error

MAX_PROBE_SECONDS = 15.0
PROBE_TOKENS = 128
# 64×64 单色 PNG，由系统内置，测试时不会上传用户材料。
PROBE_IMAGE_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAT0lEQVR42u3PQQkAAAgEsMthWhtbwQ"
    "i+hcEKLNXzWgQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQELguaCZFpwfwhwQAAAABJRU5Erk"
    "Jggg=="
)
TEST_FAILURES = {
    "MODEL_NOT_CONFIGURED": "请填写语言模型所需的 API Key 和模型 ID",
    "MODEL_AUTH_DENIED": "供应商拒绝了请求，请检查 API Key 和权限",
    "MODEL_ACCOUNT_ERROR": "请检查供应商的 API Key、账户余额或配额",
    "RATE_LIMITED": "请求触发了供应商限流，请稍后重试",
    "HTTP_400": "供应商拒绝了请求，请检查模型 ID 和能力配置",
    "HTTP_404": "供应商未找到接口或模型，请检查 Base URL 和模型 ID",
    "NETWORK_TIMEOUT": "请求超时，请检查供应商连接或稍后重试",
    "WEB_SEARCH_TIMEOUT": "搜索请求超时，请检查 Tavily 连接或稍后重试",
    "NETWORK_UNAVAILABLE": "无法连接供应商，请检查地址和后端出站网络",
    "ENDPOINT_CONFIGURATION_ERROR": "无法解析供应商域名，请检查地址和 DNS",
    "TLS_CONFIGURATION_ERROR": "供应商的 HTTPS 证书校验失败",
}


class ConfigCheck(BaseModel):
    """可展示的单项测试结果，省略供应商响应正文。"""

    check: Literal["text", "vision", "search"]
    ok: bool
    message: str


def _failure_message(error: Exception) -> str:
    """复用运行时错误分类，诊断结果不引用异常原文。"""
    cause = error.__cause__
    if isinstance(error, ApplicationError) and isinstance(cause, httpx.HTTPError):
        error = cause
    fallback = "供应商调用失败，请检查配置或稍后重试"
    try:
        return TEST_FAILURES.get(classify_error(error).code, fallback)
    except (TypeError, ValueError):
        # 供应商也可能返回格式异常的错误码，只展示固定文本。
        return fallback


async def _check_model(settings: Settings, *, vision: bool) -> ConfigCheck:
    """请求实际模型。图片测试仅发送内置样图。"""
    check: Literal["text", "vision"] = "vision" if vision else "text"
    timeout = min(settings.model_timeout_seconds, MAX_PROBE_SECONDS)
    message = HumanMessage(content="Reply with OK.")
    if vision:
        message = HumanMessage(content=[
            {"type": "text", "text": "What color is this image? Reply in one word."},
            {"type": "image_url", "image_url": {"url": PROBE_IMAGE_URL}},
        ])
    try:
        with tracing_context(enabled=False):
            async with asyncio.timeout(timeout):
                model = create_llm(settings, max_tokens=min(settings.model_max_tokens, PROBE_TOKENS), vision=vision)
                try:
                    await model.ainvoke([message])
                finally:
                    await model.root_async_client.close()
                    await asyncio.to_thread(model.root_client.close)
    except Exception as error:
        return ConfigCheck(check=check, ok=False, message=_failure_message(error))
    return ConfigCheck(check=check, ok=True, message="调用成功")


async def probe_connections(settings: Settings, group: ConfigGroup) -> list[ConfigCheck]:
    """测试指定配置组，不写入数据库或替换当前运行配置。"""
    if group == "llm":
        results = [await _check_model(settings, vision=False)]
        if settings.llm_provider == "openai_compatible" and settings.llm_vision_model:
            results.append(await _check_model(settings, vision=True))
        return results

    api_key = settings.tavily_api_key.get_secret_value().strip()
    if not api_key:
        return [ConfigCheck(check="search", ok=False, message="请填写 Tavily API Key")]
    timeout = min(settings.web_search_timeout_seconds, MAX_PROBE_SECONDS)
    try:
        async with asyncio.timeout(timeout):
            await search_tavily(api_key, "Tavily search API", 1, timeout)
    except Exception as error:
        return [ConfigCheck(check="search", ok=False, message=_failure_message(error))]
    return [ConfigCheck(check="search", ok=True, message="搜索成功")]
