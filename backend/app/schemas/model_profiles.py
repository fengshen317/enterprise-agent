"""可保存并切换的语言模型配置。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ModelProfileFields(BaseModel):
    """模型配置中可展示的字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    provider: Literal["deepseek", "openai_compatible"]
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=500)
    vision_model_id: str = Field(default="", max_length=200)


class ModelProfileDraft(ModelProfileFields):
    """每个模型使用自己的凭据，包括显式的无鉴权配置。"""

    api_key: SecretStr = Field(default=SecretStr(""), max_length=10000)

    @model_validator(mode="after")
    def validate_connection(self) -> "ModelProfileDraft":
        """检查所选协议所需的字段，复用部署配置校验实际地址。"""
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("请填写兼容接口的 Base URL")
        if self.provider == "deepseek" and (self.base_url or self.vision_model_id):
            raise ValueError("DeepSeek 官方接口使用内置地址和图文能力")
        if self.provider == "deepseek" and not self.api_key.get_secret_value().strip():
            raise ValueError("请填写 DeepSeek API Key")
        return self

    def config_values(self) -> dict[str, str]:
        """转换为当前运行时使用的配置，同时明确传入本模型的密钥值。"""
        secret = self.api_key.get_secret_value().strip()
        if self.provider == "deepseek":
            return {"llm_provider": self.provider, "deepseek_model": self.model_id, "deepseek_api_key": secret}
        return {
            "llm_provider": self.provider,
            "llm_base_url": self.base_url,
            "llm_model": self.model_id,
            "llm_vision_model": self.vision_model_id,
            "llm_api_key": secret,
        }


class ModelProfileView(ModelProfileFields):
    """系统管理员列表中只展示密钥配置状态。"""

    id: UUID
    active: bool
    has_api_key: bool


class PersonalModelDraft(ModelProfileDraft):
    """创建个人配置时，可在同一事务中选为个人默认。"""

    make_default: bool = False

    def profile(self) -> ModelProfileDraft:
        """提取通用模型字段，个人偏好不进入供应商参数。"""
        return ModelProfileDraft.model_validate(self.model_dump(exclude={"make_default"}))


class PersonalModelsView(BaseModel):
    """个人模型与空间提供的公共默认项。"""

    profiles: list[ModelProfileView]
    shared_default: ModelProfileFields
