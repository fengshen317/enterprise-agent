"""集中加载并校验环境配置。"""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置；连接凭证禁止出现在日志与响应中。"""

    model_config = SettingsConfigDict(
        env_prefix="EA_", env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    environment: Literal["development", "test", "production"] = "development"
    database_host: str = Field(default="127.0.0.1", min_length=1)
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_user: str = Field(default="postgres", min_length=1)
    database_password: SecretStr = SecretStr("")
    database_name: str = Field(default="enterprise_agent", min_length=1)
    test_database_name: str | None = None
    # 工程检查不依赖模型凭证；调用模型时再检查是否已配置。
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = Field(default="deepseek-flash", min_length=1, max_length=200)
    allowed_models: list[str] = Field(default_factory=lambda: ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"])
    reasoner_models: list[str] = Field(default_factory=lambda: ["deepseek-reasoner"])
    model_max_tokens_by_model: dict[str, int] = Field(
        default_factory=lambda: {"deepseek-flash": 4096, "deepseek-chat": 8192, "deepseek-reasoner": 16384}
    )
    model_timeout_seconds: float = Field(default=120, gt=0, le=600)
    chat_run_timeout_seconds: float = Field(default=1800, gt=0, le=3600)
    model_max_tokens: int = Field(default=4096, ge=1, le=8192)
    model_context_characters: int = Field(default=100000, ge=1000, le=500000)
    context_compression_enabled: bool = True
    context_compression_trigger_ratio: float = Field(default=0.7, gt=0, lt=1)
    context_compression_recent_turns: int = Field(default=4, ge=0, le=50)
    context_summary_characters: int = Field(default=4000, ge=100, le=10000)
    context_summary_batch_characters: int = Field(default=32000, ge=1000, le=64000)
    model_output_characters: int = Field(default=40000, ge=1000, le=200000)
    cors_origins: list[str] = ["http://localhost:8686", "http://127.0.0.1:8686"]
    tools_enabled: bool = True
    tool_timeout_seconds: float = Field(default=5, gt=0, le=30)
    tool_max_calls_per_run: int = Field(default=4, ge=1, le=10)
    task_scheduler_enabled: bool = True
    task_retry_attempts: int = Field(default=3, ge=1, le=5)
    task_retry_base_seconds: float = Field(default=2, gt=0, le=60)
    task_retry_max_seconds: float = Field(default=30, gt=0, le=300)
    task_retry_max_wait_seconds: float = Field(default=300, gt=0, le=1800)
    task_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    task_heartbeat_seconds: float = Field(default=5, gt=0, le=10)
    task_parallel_steps: int = Field(default=1, ge=1, le=2)
    task_concurrent_runs: int = Field(default=2, ge=1, le=4)
    # 定时问答：单进程调度，按 Cron 向固定对话追加新运行。
    schedule_enabled: bool = True
    schedule_poll_seconds: float = Field(default=5, gt=0, le=60)
    schedule_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    # 知识库：RAGFlow 只作为解析与检索底座，访问凭证只在后端。
    knowledge_enabled: bool = True
    ragflow_base_url: str = Field(default="http://localhost", min_length=1, max_length=300)
    ragflow_api_key: SecretStr = SecretStr("")
    ragflow_timeout_seconds: float = Field(default=15, gt=0, le=120)
    ragflow_parser_id: str = Field(default="general", min_length=1, max_length=64)
    ragflow_parse_type: int = Field(default=1, ge=1, le=16)
    ragflow_embedding_model: str = Field(default="", max_length=200)
    ragflow_language: str = Field(default="", max_length=64)
    knowledge_top_k: int = Field(default=5, ge=1, le=20)
    knowledge_test_top_k: int = Field(default=20, ge=1, le=50)
    knowledge_similarity_threshold: float = Field(default=0.2, ge=0, le=1)
    knowledge_max_context_characters: int = Field(default=8000, ge=1000, le=50000)
    knowledge_max_calls_per_run: int = Field(default=3, ge=1, le=10)
    knowledge_max_file_bytes: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    # 自动知识库检索注入：每次提问自动检索并注入参考资料。
    auto_retrieve_max_characters: int = Field(default=4000, ge=500, le=20000)
    # 联网搜索：Tavily API，密钥只在后端。
    tavily_api_key: SecretStr = SecretStr("")
    web_search_timeout_seconds: float = Field(default=10, gt=0, le=60)
    web_search_max_results: int = Field(default=5, ge=1, le=10)
    # Agent 工作台：能力装配、技能预算与写操作审批。
    agent_max_tools: int = Field(default=20, ge=1, le=50)
    agent_instructions_characters: int = Field(default=8000, ge=100, le=20000)
    skill_context_characters: int = Field(default=12000, ge=1000, le=50000)
    skill_always_on_max: int = Field(default=3, ge=0, le=10)
    skill_load_max_characters: int = Field(default=6000, ge=500, le=20000)
    skill_load_max_calls: int = Field(default=3, ge=1, le=10)
    approval_timeout_seconds: float = Field(default=300, gt=0, le=1800)
    tool_output_characters: int = Field(default=8000, ge=1000, le=50000)
    # 微信 iLink 渠道。未配置密钥时不轮询、不提供绑定接口。
    # channel_version 前三段必须是 openclaw-weixin 插件版本，宿主版本放在斜杠后面。
    channel_enabled: bool = True
    channel_token_key: SecretStr = SecretStr("")
    channel_reply_window_hours: int = Field(default=24, ge=1, le=168)
    channel_window_reminder_hours: int = Field(default=2, ge=0, le=23)
    channel_progress_after_seconds: float = Field(default=10, ge=0, le=120)
    channel_progress_interval_seconds: float = Field(default=20, ge=0.01, le=120)
    channel_reply_preview_characters: int = Field(default=500, ge=80, le=1800)
    public_app_base_url: str = Field(default="http://127.0.0.1:8686", min_length=1, max_length=300)
    ilink_base_url: str = Field(default="https://ilinkai.weixin.qq.com", min_length=1, max_length=300)
    ilink_channel_version: str = Field(default="3.0.0/enterprise-agent-0.1.0", min_length=5, max_length=128)
    ilink_bot_agent: str = Field(default="EnterpriseAgent/0.1.0", min_length=1, max_length=256)
    ilink_session_pause_seconds: int = Field(default=3600, ge=1, le=86400)
    ilink_long_poll_seconds: float = Field(default=35, gt=1, le=120)
    ilink_message_chunk_characters: int = Field(default=1800, ge=100, le=5000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    auth_session_hours: int = Field(default=12, ge=1, le=168)
    auth_cookie_secure: bool = False
    auth_login_max_failures: int = Field(default=5, ge=1, le=20)
    auth_login_window_seconds: int = Field(default=900, ge=60, le=3600)
    auth_trusted_origins: list[str] = [
        "http://127.0.0.1:8686",
        "http://localhost:8686",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ]
    database_timeout_seconds: float = Field(default=3, gt=0, le=30)

    @field_validator("allowed_models", "reasoner_models", mode="before")
    @classmethod
    def parse_model_list(cls, value: object) -> object:
        """接受 JSON 数组或逗号分隔的模型名。

        Args:
            value: 环境变量或默认列表。

        Returns:
            模型名列表。
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                parsed = json.loads(text)
                if not isinstance(parsed, list):
                    raise ValueError("模型列表必须是数组")
                return parsed
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("allowed_models", "reasoner_models")
    @classmethod
    def clean_model_list(cls, value: list[str]) -> list[str]:
        """去掉空白项，并拒绝空的允许列表。

        Args:
            value: 已解析的模型名。

        Returns:
            去重后仍保持原顺序的模型名。
        """
        cleaned: list[str] = []
        for item in value:
            name = item.strip()
            if name and name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise ValueError("模型列表不能为空")
        return cleaned

    @field_validator("model_max_tokens_by_model", mode="before")
    @classmethod
    def parse_token_caps(cls, value: object) -> object:
        """接受 JSON 对象形式的分模型输出上限。

        Args:
            value: 环境变量或默认字典。

        Returns:
            模型名到 token 上限的映射。
        """
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("分模型输出上限必须是对象")
            return parsed
        return value

    @field_validator("model_max_tokens_by_model")
    @classmethod
    def validate_token_caps(cls, value: dict[str, int]) -> dict[str, int]:
        """限制每个模型的输出上限，推理模型可以高于全局默认值。

        Args:
            value: 模型名到 token 上限。

        Returns:
            校验后的映射。
        """
        cleaned: dict[str, int] = {}
        for name, cap in value.items():
            key = str(name).strip()
            number = int(cap)
            if not key or number < 1 or number > 128000:
                raise ValueError("分模型输出上限必须在 1 到 128000 之间")
            cleaned[key] = number
        return cleaned

    @field_validator("database_host", "database_user", "database_name")
    @classmethod
    def validate_database_part(cls, value: str) -> str:
        """拒绝空白连接参数，不对密码执行裁剪。

        Args:
            value: 地址、用户名或数据库名。

        Returns:
            去除首尾空白后的参数。
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("数据库地址、用户名和数据库名不能为空")
        return cleaned

    @property
    def sqlalchemy_url(self) -> URL:
        """由独立参数构造连接对象，保留密码中的特殊字符。

        Returns:
            可直接传给 SQLAlchemy 的连接对象，默认字符串表示隐藏密码。
        """
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )

    @property
    def test_sqlalchemy_url(self) -> URL:
        """只替换数据库名，并阻止测试连接到开发库。

        Returns:
            共用连接参数的测试库连接对象。

        Raises:
            ValueError: 未配置测试库或测试库名称不安全。
        """
        name = (self.test_database_name or "").strip()
        if not name or not name.endswith("_test") or name == self.database_name:
            raise ValueError("测试库名必须以 _test 结尾，且不能与开发库相同")
        return self.sqlalchemy_url.set(database=name)
