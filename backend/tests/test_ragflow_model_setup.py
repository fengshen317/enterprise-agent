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


@pytest.mark.parametrize(("provider_error", "expected_code"), [
    (None, ""),
    ("SILICONFLOW API error: 401 Unauthorized, body: fixture-key", "RAGFLOW_MODEL_API_KEY_REJECTED"),
    ("URL resolves to a non-public address (198.18.0.92), which is not allowed", "RAGFLOW_MODEL_DNS_BLOCKED"),
])
async def test_siliconflow_uses_native_embedding_connection_probe(
    provider_error: str | None, expected_code: str, caplog: pytest.LogCaptureFixture
) -> None:
    """固定维度模型用 RAGFlow 的原生测试，并隐藏供应商原始报错。"""
    requests: list[tuple[str, str]] = []
    path = "/api/v1/providers/SILICONFLOW"

    def upstream(request: httpx.Request) -> httpx.Response:
        """模拟 RAGFlow 连接检查，可返回包含密钥的上游报错。"""
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/v1/providers":
            return httpx.Response(200, json={"code": 0, "data": [{"name": "SILICONFLOW"}]})
        if request.url.path == f"{path}/connection":
            assert json.loads(request.content) == {
                "api_key": "fixture-key",
                "model_info": [{"model_name": "BAAI/bge-m3", "model_type": ["embedding"]}],
            }
            if provider_error:
                return httpx.Response(200, json={"code": 500, "message": provider_error})
        return httpx.Response(200, json={"code": 0})

    async with httpx.AsyncClient(base_url="http://ragflow", transport=httpx.MockTransport(upstream)) as http_client:
        client = RagflowClient(Settings(ragflow_api_key=SecretStr("fixture-rag-key")), http_client)
        service = RagflowModelService(client, "")
        setup = EmbeddingSetup(preset="siliconflow_bge_m3", api_key=SecretStr("fixture-key"))
        if provider_error:
            with pytest.raises(ApplicationError) as caught:
                await service.configure(setup)
            assert caught.value.code == expected_code
            assert "fixture-key" not in str(caught.value)
            assert "fixture-key" not in caplog.text
            assert requests == [("GET", "/api/v1/providers"), ("POST", f"{path}/connection")]
        else:
            result = await service.configure(setup)
            assert result.model is not None and result.model.name == "BAAI/bge-m3"
            assert requests == [
                ("GET", "/api/v1/providers"),
                ("POST", f"{path}/connection"),
                ("POST", f"{path}/instances"),
                ("PATCH", "/api/v1/models/default"),
            ]
