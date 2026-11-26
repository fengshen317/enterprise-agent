"""从部署内置的供应商地址读取模型列表。"""

import asyncio
import json
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.errors import ApplicationError

CATALOG_TIMEOUT = 12.0
CATALOG_BYTES = 2 * 1024 * 1024
MODEL_ID_MAX_LENGTH = 200


class ModelProviderPreset(BaseModel):
    """前端添加模型所需的供应商和推理地址。"""

    id: str
    label: str
    base_url: str
    docs_url: str
    supports_discovery: bool


class ModelCatalogRequest(BaseModel):
    """模型发现只接收凭据，访问地址由服务端确定。"""

    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=1, max_length=10000)


@dataclass(frozen=True)
class _Provider:
    """保存推理端点与已核实的模型列表端点。"""

    label: str
    base_url: str
    docs_url: str
    models_path: str = ""


_PROVIDERS = {
    "mimo": _Provider(
        "小米 MiMo", "https://api.xiaomimimo.com/v1", "https://mimo.mi.com/docs/zh-CN/api/model/list-models", "/models"
    ),
    "deepseek": _Provider(
        "DeepSeek", "https://api.deepseek.com", "https://api-docs.deepseek.com/api/list-models/", "/models"
    ),
    "siliconflow": _Provider(
        "硅基流动",
        "https://api.siliconflow.cn/v1",
        "https://api-docs.siliconflow.cn/docs/api/models-get",
        "/models?sub_type=chat",
    ),
    "tencent": _Provider(
        "腾讯云 TokenHub",
        "https://tokenhub.tencentmaas.com/v1",
        "https://cloud.tencent.com/document/product/1823/130078",
        "/models",
    ),
    "tencent-sg": _Provider(
        "腾讯云 TokenHub（新加坡）",
        "https://tokenhub-intl.tencentmaas.com/v1",
        "https://cloud.tencent.com/document/product/1823/130079",
        "/models",
    ),
    "openai": _Provider(
        "OpenAI",
        "https://api.openai.com/v1",
        "https://developers.openai.com/api/reference/resources/models/methods/list",
        "/models",
    ),
    "openrouter": _Provider(
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties",
        "/models",
    ),
    "kimi": _Provider(
        "Kimi 中国版",
        "https://api.moonshot.cn/v1",
        "https://platform.moonshot.cn/docs/guide/start-using-kimi-api",
        "/models",
    ),
    "zhipu": _Provider("智谱开放平台", "https://open.bigmodel.cn/api/paas/v4", "https://docs.bigmodel.cn/"),
    "minimax": _Provider(
        "MiniMax 中国版", "https://api.minimax.cn/v1", "https://platform.minimax.cn/docs/api-reference/text-openai-api"
    ),
}


def list_model_providers() -> list[ModelProviderPreset]:
    """列出可以预填地址的供应商，并标记是否支持查询。"""
    return [
        ModelProviderPreset(
            id=key,
            label=item.label,
            base_url=item.base_url,
            docs_url=item.docs_url,
            supports_discovery=bool(item.models_path),
        )
        for key, item in _PROVIDERS.items()
    ]


def _check_response(status: int) -> None:
    """把上游失败转成固定提示，避免返回上游正文或凭据。"""
    if status in {401, 403}:
        raise ApplicationError("MODEL_CATALOG_AUTH_DENIED", "厂商拒绝了请求，请检查 API Key、区域和权限", 422)
    if status == 429:
        raise ApplicationError("MODEL_CATALOG_RATE_LIMITED", "厂商限制了请求频率，请稍后重试", 429)
    if status != 200:
        raise ApplicationError(
            "MODEL_CATALOG_UNAVAILABLE", "厂商暂时无法返回模型列表，可稍后重试或手动填写模型 ID", 502
        )


async def discover_model_ids(
    provider_id: str, api_key: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[str]:
    """查询指定厂商并返回去重后的模型 ID。"""
    preset = _PROVIDERS.get(provider_id)
    if preset is None or not preset.models_path:
        raise ApplicationError("MODEL_CATALOG_UNSUPPORTED", "此厂商需要手动填写模型 ID", 422)
    key = api_key.get_secret_value().strip()
    if not key or not key.isascii() or any(char.isspace() or not char.isprintable() for char in key):
        raise ApplicationError("MODEL_CATALOG_INVALID_KEY", "请填写有效的 API Key", 422)
    try:
        async with (
            asyncio.timeout(CATALOG_TIMEOUT),
            httpx.AsyncClient(transport=transport, follow_redirects=False) as client,
            client.stream(
                "GET", preset.base_url + preset.models_path, headers={"Authorization": f"Bearer {key}"}
            ) as response,
        ):
            _check_response(response.status_code)
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > CATALOG_BYTES:
                    raise ApplicationError("MODEL_CATALOG_INVALID", "厂商返回的模型列表过大，请手动填写模型 ID", 502)
                payload.extend(chunk)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ApplicationError("MODEL_CATALOG_TIMEOUT", "读取厂商模型列表超时，请稍后重试", 504) from exc
    except httpx.RequestError as exc:
        raise ApplicationError("MODEL_CATALOG_UNAVAILABLE", "无法连接所选厂商，请检查后端出站网络", 502) from exc

    try:
        result = json.loads(payload)
    except (ValueError, RecursionError) as exc:
        raise ApplicationError("MODEL_CATALOG_INVALID", "厂商返回的模型列表格式无效", 502) from exc
    items = result.get("data") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise ApplicationError("MODEL_CATALOG_INVALID", "厂商返回的模型列表格式无效", 502)
    models = set()
    for item in items:
        identifier = item.get("id") if isinstance(item, dict) else None
        if isinstance(identifier, str) and 0 < len(identifier) <= MODEL_ID_MAX_LENGTH and identifier.isprintable():
            if identifier.strip() == identifier:
                models.add(identifier)
    return sorted(models, key=lambda model: (model.casefold(), model))
