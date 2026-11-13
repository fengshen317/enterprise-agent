"""模型白名单与思考过程提取，不访问网络。"""

from types import SimpleNamespace

import pytest

from app.agents.provider import read_prompt_tokens, read_reasoning
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.services.model_catalog import is_reasoner, max_tokens_for, resolve_model, summary_model_for, thinking_extra


def test_resolve_model_prefers_conversation_then_agent_then_default() -> None:
    """显式对话模型优先，非法 Agent 模型回退到全局默认。"""
    settings = Settings(_env_file=None)
    assert resolve_model(settings, "deepseek-chat", "deepseek-flash") == "deepseek-chat"
    assert resolve_model(settings, None, "deepseek-reasoner") == "deepseek-reasoner"
    assert resolve_model(settings, "  ", "missing-model") == "deepseek-flash"
    with pytest.raises(ApplicationError) as denied:
        resolve_model(settings, "gpt-4", None)
    assert denied.value.code == "MODEL_NOT_ALLOWED"


def test_reasoner_uses_its_own_token_cap_and_stays_out_of_summaries() -> None:
    """推理模型打开思考并使用单独输出上限，摘要改走非推理模型。"""
    settings = Settings(_env_file=None, allowed_models="deepseek-flash,deepseek-reasoner")
    assert is_reasoner(settings, "deepseek-reasoner")
    assert not is_reasoner(settings, "deepseek-flash")
    assert max_tokens_for(settings, "deepseek-reasoner") == 16384
    assert max_tokens_for(settings, "deepseek-flash") == 4096
    assert thinking_extra(settings, "deepseek-reasoner") == {"thinking": {"type": "enabled"}}
    assert thinking_extra(settings, "deepseek-flash") == {"thinking": {"type": "disabled"}}
    assert summary_model_for(settings, "deepseek-reasoner") == "deepseek-flash"


def test_read_reasoning_and_prompt_tokens() -> None:
    """供应商把思考和用量放在附加字段时可以读出。"""
    message = SimpleNamespace(
        additional_kwargs={"reasoning_content": "先拆问题"},
        usage_metadata={"input_tokens": 42},
        response_metadata={},
    )
    assert read_reasoning(message) == "先拆问题"
    assert read_prompt_tokens(message) == 42
    assert read_reasoning(SimpleNamespace()) == ""
    assert read_prompt_tokens(SimpleNamespace(response_metadata={"token_usage": {"prompt_tokens": 7}})) == 7
