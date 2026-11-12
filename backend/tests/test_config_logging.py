"""配置校验与日志公开字段测试。"""

import json
import logging

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import JsonFormatter


def test_settings_validates_port_and_timeout() -> None:
    """拒绝无效端口与无限超时。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_port=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_port=65536)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_name="   ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_timeout_seconds=0)
    assert "enterprise_local" not in repr(Settings(_env_file=None))


def test_database_parts_preserve_password_and_hide_it() -> None:
    """独立字段不需要 URL 编码，公开表示不能泄露密码。"""
    password = " p@ss:#/%'with-space "
    settings = Settings(
        _env_file=None,
        database_host="localhost",
        database_port=5433,
        database_user="postgres",
        database_password=password,
        database_name="agent_test",
    )
    url = settings.sqlalchemy_url
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "localhost"
    assert url.port == 5433
    assert url.username == "postgres"
    assert url.database == "agent_test"
    assert url.password == password
    assert password not in repr(settings)
    assert password not in str(url)


def test_database_parts_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境配置覆盖默认值且密码按原文加载。

    Args:
        monkeypatch: 隔离环境变量的测试夹具。
    """
    monkeypatch.setenv("EA_DATABASE_HOST", "localhost")
    monkeypatch.setenv("EA_DATABASE_PORT", "5434")
    monkeypatch.setenv("EA_DATABASE_USER", "postgres")
    monkeypatch.setenv("EA_DATABASE_PASSWORD", "p@ss#/%")
    monkeypatch.setenv("EA_DATABASE_NAME", "environment_test")
    settings = Settings(_env_file=None)
    assert settings.sqlalchemy_url.port == 5434
    assert settings.sqlalchemy_url.password == "p@ss#/%"
    assert settings.sqlalchemy_url.database == "environment_test"


def test_json_log_keeps_correlation_without_arbitrary_fields() -> None:
    """只输出允许的字段，避免附加数据泄露。"""
    record = logging.LogRecord("app", logging.INFO, "", 0, "http_request", (), None)
    record.request_id = "request-test"
    record.password = "private-password"
    output = JsonFormatter().format(record)
    assert json.loads(output)["request_id"] == "request-test"
    assert "private-password" not in output


def test_test_database_shares_credentials_but_is_isolated() -> None:
    """测试库复用账号和地址，但不能指向开发库。"""
    settings = Settings(
        _env_file=None,
        database_name="agent",
        test_database_name="agent_test",
        database_password="p@ss#/%",
    )
    assert settings.test_sqlalchemy_url.database == "agent_test"
    assert settings.test_sqlalchemy_url.set(database="agent") == settings.sqlalchemy_url
    for name in (None, "agent", "   "):
        with pytest.raises(ValueError):
            _ = Settings(_env_file=None, test_database_name=name).test_sqlalchemy_url
    with pytest.raises(ValueError):
        _ = Settings(_env_file=None, database_name="same_test", test_database_name="same_test").test_sqlalchemy_url


def test_deepseek_key_loads_without_exposing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证密钥通过环境加载，配置展示保持脱敏。

    Args:
        monkeypatch: 隔离环境变量，避免使用开发密钥。
    """
    monkeypatch.delenv("EA_DEEPSEEK_API_KEY", raising=False)
    assert Settings(_env_file=None).deepseek_api_key.get_secret_value() == ""
    key = "test-deepseek-key-not-a-real-secret"
    monkeypatch.setenv("EA_DEEPSEEK_API_KEY", key)
    settings = Settings(_env_file=None)
    assert settings.deepseek_api_key.get_secret_value() == key
    assert key not in repr(settings)
    assert key not in settings.model_dump_json()


@pytest.mark.parametrize(
    "fields",
    [
        {"context_compression_trigger_ratio": 0},
        {"context_compression_trigger_ratio": 1},
        {"context_compression_recent_turns": -1},
        {"context_summary_characters": 0},
        {"context_summary_batch_characters": 999},
    ],
)
def test_compression_configuration_rejects_invalid_values(fields: dict[str, object]) -> None:
    """压缩配置不能使用无效阈值或消息预算。

    Args:
        fields: 待拒绝的配置参数。
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **fields)
