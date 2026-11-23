"""覆盖模型升级顺序及探测失败时对现有默认模型的保护。"""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.schemas.ragflow_models import EmbeddingSetup
from app.services.ragflow import RagflowClient
from app.services.ragflow_models import RagflowModelService


def setup_model() -> EmbeddingSetup:
    """用可控的兼容服务参数覆盖自定义维度路径。"""
    return EmbeddingSetup(
        preset="compatible",
        api_key=SecretStr("fixture-key"),
        base_url="https://example.invalid/v1",
        model_name="fixture-model",
        dimension=3,
        batch_size=32,
    )


@pytest.mark.parametrize("vector", [[0.1, 0.2, 0.3], [0.1, 0.2]])
async def test_embedding_probe_precedes_default_switch(vector: list[float]) -> None:
    """只有成功得到指定维度后才提交默认模型。"""
    requests: list[tuple[str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        """模拟已注册供应商与可控探测结果。"""
        path = request.url.path
        requests.append((request.method, path))
        data: Any = None
        if path == "/api/v1/providers":
            data = [{"name": "OpenAI-API-Compatible"}]
        elif path == "/api/v1/embeddings":
            assert json.loads(request.content)["dimension"] == 3
            data = [{"embedding": vector}]
        return httpx.Response(200, json={"code": 0, "data": data})

    async with httpx.AsyncClient(
        base_url="http://ragflow", transport=httpx.MockTransport(upstream)
    ) as http_client:
        client = RagflowClient(Settings(ragflow_api_key=SecretStr("fixture-rag-key")), http_client)
        service = RagflowModelService(client, "")
        if len(vector) == 3:
            result = await service.configure(setup_model())
            assert result.model is not None and result.model.name == "fixture-model"
            assert requests[-1] == ("PATCH", "/api/v1/models/default")
        else:
            with pytest.raises(ApplicationError, match="维度"):
                await service.configure(setup_model())
            assert requests[-1] == ("DELETE", "/api/v1/providers/OpenAI-API-Compatible/instances")
        assert requests[:3] == [
            ("GET", "/api/v1/providers"),
            ("POST", "/api/v1/providers/OpenAI-API-Compatible/instances"),
            ("POST", "/api/v1/embeddings"),
        ]
