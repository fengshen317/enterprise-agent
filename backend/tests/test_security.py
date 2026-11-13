"""口令与 CSRF 边界验证，不使用任何用户实际凭证。"""

import pytest

from app.core.errors import ApplicationError
from app.core.security import csrf_token, digest_token, hash_password, normalize_username, verify_password


def test_password_hashing_and_invalid_hashes() -> None:
    """真实 Argon2id 哈希不能以明文替代，并拒绝错误或损坏值。"""
    password = "test-long-password-123!"
    first, second = hash_password(password), hash_password(password)
    assert first.startswith("$argon2id$") and first != second
    assert password not in first and verify_password(password, first)
    assert not verify_password("wrong", first)
    assert not verify_password(password, None)
    assert not verify_password(password, "corrupt-hash")
    with pytest.raises(ApplicationError):
        normalize_username("bad name")
    assert normalize_username(" ALICE_1 ") == "alice_1"


def test_csrf_and_session_digests_are_distinct_and_session_bound() -> None:
    """不同用途摘要不能互相替代，不同会话的 CSRF 值不同。"""
    token = "random-test-session-only"
    assert csrf_token(token) != digest_token(token)
    assert csrf_token(token) != csrf_token("another-test-session")
    assert len(digest_token(token)) == 64 and token not in digest_token(token)


@pytest.mark.parametrize("password", ["1", "", "   ", "中文🙂", "x" * 129])
def test_password_has_no_character_limits(password: str) -> None:
    """创建与登录使用同一原始值，不裁剪或拒绝字符。

    Args:
        password: 不同长度与字符组成的测试密码。
    """
    from app.schemas.auth import LoginRequest

    request = LoginRequest(username="admin", password=password)
    assert request.password.get_secret_value() == password
    assert verify_password(password, hash_password(password))
