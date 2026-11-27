"""检查模型目录固定地址、鉴权和错误脱敏。"""

import httpx
import pytest
from pydantic import SecretStr

from app.core.errors import ApplicationError
from app.services.model_catalog import discover_model_ids


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("mimo", "https://api.xiaomimimo.com/v1/models"),
        ("deepseek", "https://api.deepseek.com/models"),
        ("siliconflow", "https://api.siliconflow.cn/v1/models?sub_type=chat"),
        ("tencent", "https://tokenhub.tencentmaas.com/v1/models"),
        ("tencent-sg", "https://tokenhub-intl.tencentmaas.com/v1/models"),
        ("openai", "https://api.openai.com/v1/models"),
        ("openrouter", "https://openrouter.ai/api/v1/models"),
        ("kimi", "https://api.moonshot.cn/v1/models"),
    ],
)
async def test_provider_catalog_uses_official_url(provider: str, url: str) -> None:
    """仅把对应凭据交给所选厂商，并去重返回有效模型。"""

    def send(request: httpx.Request) -> httpx.Response:
        """验证目标地址与鉴权头，模拟厂商目录。"""
        assert str(request.url) == url
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer fixture-key"
        data = [{"id": "demo-z"}, {"id": "demo-a"}, {"id": "demo-z"}, {"id": ""}, {"id": "control\n"}]
        return httpx.Response(200, json={"data": data})

    assert await discover_model_ids(provider, SecretStr(" fixture-key "), transport=httpx.MockTransport(send)) == [
        "demo-a",
        "demo-z",
    ]


@pytest.mark.parametrize(
    ("status", "code"),
    [(302, "MODEL_CATALOG_UNAVAILABLE"), (401, "MODEL_CATALOG_AUTH_DENIED"), (429, "MODEL_CATALOG_RATE_LIMITED")],
)
async def test_provider_catalog_does_not_follow_redirects_or_echo_errors(status: int, code: str) -> None:
    """重定向不会转交凭据，错误也不会包含原始响应。"""
    calls = 0

    def send(request: httpx.Request) -> httpx.Response:
        """模拟携带敏感文本和外部重定向的错误。"""
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="private-upstream-error", headers={"Location": "https://elsewhere.invalid/"})

    with pytest.raises(ApplicationError) as exc:
        await discover_model_ids("mimo", SecretStr("fixture-key"), transport=httpx.MockTransport(send))
    assert calls == 1 and exc.value.code == code and "private-upstream-error" not in exc.value.message


async def test_provider_catalog_requires_preset_and_nonempty_key() -> None:
    """未知厂商和无效密钥在发出上游请求前就被拒绝。"""
    with pytest.raises(ApplicationError, match="手动填写"):
        await discover_model_ids("custom", SecretStr("fixture-key"))
    with pytest.raises(ApplicationError, match="API Key"):
        await discover_model_ids("mimo", SecretStr("invalid\nkey"))
