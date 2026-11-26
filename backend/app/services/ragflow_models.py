"""将工作台的模型配置映射到 RAGFlow v1.0.0 的租户接口。"""

import asyncio
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from app.core.errors import ApplicationError
from app.schemas.ragflow_models import ActiveEmbedding, EmbeddingSetup, EmbeddingStatus
from app.services.ragflow import RagflowClient

PREFIX = "/api/v1"
PROVIDER_PRESETS = {
    "siliconflow_bge_m3": ("SILICONFLOW", "BAAI/bge-m3", 1024, 32),
    "openai_small": ("OpenAI", "text-embedding-3-small", 1536, 32),
}
PROBE_TEXT = "Enterprise Agent knowledge embedding check"


class RagflowModelService:
    """只在探测成功后切换默认模型，保留旧库依赖的实例。"""

    def __init__(self, client: RagflowClient, environment_override: str) -> None:
        """绑定普通服务账号客户端及部署指定的覆盖模型。"""
        self.client = client
        self.environment_override = environment_override
        self._lock = asyncio.Lock()

    async def status(self) -> EmbeddingStatus:
        """读取当前租户的默认向量模型，过滤上游敏感字段。"""
        result = await self.client.model_request("GET", f"{PREFIX}/models/default")
        rows = result.get("models") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ApplicationError("RAGFLOW_MODEL_RESPONSE_INVALID", "无法读取 RAGFlow 默认模型", 502)
        for item in rows:
            if not isinstance(item, dict) or item.get("model_type") != "embedding" or item.get("enable") is False:
                continue
            provider, name, instance = item.get("model_provider"), item.get("model_name"), item.get("model_instance")
            if isinstance(provider, str) and isinstance(name, str) and name:
                model = ActiveEmbedding(
                    provider=provider, name=name, instance=instance if isinstance(instance, str) else ""
                )
                return EmbeddingStatus(state="ready", model=model, environment_override=self.environment_override)
        return EmbeddingStatus(state="needs_model", environment_override=self.environment_override)

    async def configure(self, setup: EmbeddingSetup) -> EmbeddingStatus:
        """创建候选模型，验证真实调用，再更新 RAGFlow 默认设置。"""
        if self.environment_override:
            raise ApplicationError(
                "RAGFLOW_EMBEDDING_PINNED", "请先移除部署中的 EA_RAGFLOW_EMBEDDING_MODEL 覆盖", 409
            )
        if setup.preset == "compatible":
            assert setup.dimension is not None and setup.batch_size is not None
            provider, name, dimension, batch_size = (
                "OpenAI-API-Compatible", setup.model_name, setup.dimension, setup.batch_size
            )
        else:
            provider, name, dimension, batch_size = PROVIDER_PRESETS[setup.preset]
        instance = f"ea-embedding-{uuid4().hex[:12]}"
        path = f"{PREFIX}/providers/{quote(provider, safe='')}/instances"
        body: dict[str, Any] = {
            "instance_name": instance,
            "api_key": setup.api_key.get_secret_value().strip(),
            "model_info": [{
                "model_name": name,
                "model_type": ["embedding"],
                "max_tokens": setup.max_tokens,
                "extra": {"max_dimension": dimension, "max_batch_size": batch_size, "dimensions": [dimension]},
            }],
        }
        if setup.preset == "compatible":
            body["base_url"] = setup.base_url.rstrip("/")

        async with self._lock:
            await self._ensure_provider(provider)
            native_probe = setup.preset == "siliconflow_bge_m3"
            if native_probe:
                # bge-m3 使用固定维度。通用 embeddings 接口会将维度转发给供应商，
                # 而 RAGFlow 的原生连接测试和实际入库调用都使用模型的默认输出。
                try:
                    await self.client.model_request("POST", f"{PREFIX}/providers/{provider}/connection", {
                        "api_key": body["api_key"],
                        "model_info": [{"model_name": name, "model_type": ["embedding"]}],
                    })
                except ApplicationError as exc:
                    if exc.code != "RAGFLOW_REJECTED":
                        raise
                    raise ApplicationError(
                        "RAGFLOW_EMBEDDING_PROBE_FAILED",
                        "硅基流动连接测试失败，请检查 API Key、模型权限和 RAGFlow 容器的外网连接",
                        422,
                    ) from exc
            await self.client.model_request("POST", path, body)
            if not native_probe:
                await self._verify_dimension(path, provider, instance, name, dimension)
            # 默认更新超时时无法确定是否已提交，保留候选以免删除当前默认模型。
            await self.client.model_request("PATCH", f"{PREFIX}/models/default", {
                "model_provider": provider, "model_instance": instance, "model_name": name, "model_type": "embedding"
            })
            return EmbeddingStatus(
                state="ready", model=ActiveEmbedding(provider=provider, name=name, instance=instance)
            )

    async def _verify_dimension(self, path: str, provider: str, instance: str, name: str, dimension: int) -> None:
        """用候选实例检查向量长度，失败时删除候选以保留当前默认配置。"""
        try:
            result = await self.client.model_request("POST", f"{PREFIX}/embeddings", {
                "provider_name": provider,
                "instance_name": instance,
                "model_name": name,
                "texts": [PROBE_TEXT],
                "dimension": dimension,
            })
            row = result[0] if isinstance(result, list) and result else None
            vector = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vector, list) or len(vector) != dimension:
                raise ApplicationError("RAGFLOW_EMBEDDING_DIMENSION", "模型返回的向量维度与配置不一致", 422)
        except ApplicationError as exc:
            await self._discard_candidate(path, instance)
            if exc.code != "RAGFLOW_REJECTED":
                raise
            raise ApplicationError(
                "RAGFLOW_EMBEDDING_PROBE_FAILED", "Embedding 测试失败，请检查供应商、密钥、模型和连接地址", 422
            ) from exc

    async def _ensure_provider(self, provider: str) -> None:
        """新租户必须先登记供应商，已有供应商可直接复用。"""
        rows = await self.client.model_request("GET", f"{PREFIX}/providers")
        if not isinstance(rows, list):
            raise ApplicationError("RAGFLOW_MODEL_RESPONSE_INVALID", "无法读取 RAGFlow 供应商", 502)
        if any(isinstance(row, dict) and row.get("name") == provider for row in rows):
            return
        await self.client.model_request("PUT", f"{PREFIX}/providers", {"provider_name": provider})

    async def _discard_candidate(self, path: str, instance: str) -> None:
        """探测失败时尽力释放本次创建的候选模型。"""
        try:
            await self.client.model_request("DELETE", path, {"instances": [instance]})
        except ApplicationError:
            # 清理失败不能掩盖原始的连接错误。
            pass
