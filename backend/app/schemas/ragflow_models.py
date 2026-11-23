"""工作台管理知识库模型的公开响应与输入。"""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class EmbeddingSetup(BaseModel):
    """选择内置模型或明确指定兼容接口的向量参数。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    preset: Literal["siliconflow_bge_m3", "openai_small", "compatible"]
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4096)
    base_url: str = Field(default="", max_length=512)
    model_name: str = Field(default="", max_length=200)
    dimension: int | None = Field(default=None, ge=1, le=16384)
    batch_size: int | None = Field(default=None, ge=1, le=2048)
    max_tokens: int = Field(default=8192, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def validate_provider(self) -> Self:
        """保证内置供应商凭据和自定义模型参数完整。"""
        if self.preset != "compatible":
            if not self.api_key.get_secret_value().strip():
                raise ValueError("供应商需要 API Key")
            return self
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("请输入不带凭据、查询参数或片段的 HTTP(S) 地址")
        if not self.model_name or self.dimension is None or self.batch_size is None:
            raise ValueError("请填写模型名称、向量维度和批量上限")
        return self


class ActiveEmbedding(BaseModel):
    """当前默认模型的显示信息。"""

    provider: str
    name: str
    instance: str


class EmbeddingStatus(BaseModel):
    """部署连接状态及可显示的模型信息。"""

    state: Literal["not_connected", "needs_model", "ready"]
    model: ActiveEmbedding | None = None
    environment_override: str = ""
