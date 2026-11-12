"""口令哈希、会话令牌和 CSRF 工具，不记录凭证。"""

import hashlib
import hmac
import re
from functools import lru_cache

from pwdlib import PasswordHash

from app.core.errors import ApplicationError

COOKIE_NAME = "ea_session"


@lru_cache(maxsize=1)
def password_hasher() -> PasswordHash:
    """复用成熟 Argon2id 组件。

    Returns:
        密码处理器。
    """
    return PasswordHash.recommended()


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """未知账号同样执行哈希验证，减少明显的账号枚举时间差。

    Returns:
        不属于任何真实账号的哈希。
    """
    return password_hasher().hash("unusable-dummy-password-for-timing-only")


def digest_token(token: str) -> str:
    """摘要化高熵随机会话令牌。

    Args:
        token: 浏览器持有的令牌。

    Returns:
        可供数据库索引的 SHA-256 摘要。
    """
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token: str) -> str:
    """从不可被脚本读取的会话令牌派生同源 CSRF 校验值。

    Args:
        token: HttpOnly Cookie 中的随机令牌。

    Returns:
        仅向已登录同源页面返回的 CSRF 值。
    """
    return hmac.new(token.encode(), b"enterprise-agent-csrf-v1", hashlib.sha256).hexdigest()


def normalize_username(value: str) -> str:
    """生成唯一的小写账号名。

    Args:
        value: 操作者输入。

    Returns:
        合法账号名。
    """
    value = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", value) is None:
        raise ApplicationError("INVALID_USERNAME", "账号需为 3–64 位字母、数字或 _.-", 422)
    return value


def hash_password(password: str) -> str:
    """按原始输入生成哈希，不限制长度或字符组成。

    Args:
        password: 本机输入的新密码。

    Returns:
        Argon2id 编码哈希。
    """
    return password_hasher().hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    """无效哈希与未知账号统一拒绝，不把密码学异常泄露给接口。

    Args:
        password: 登录口令。
        encoded: 数据库哈希或未知账号的空值。

    Returns:
        是否匹配真实账号口令。
    """
    try:
        valid = password_hasher().verify(password, encoded or dummy_hash())
        return valid and encoded is not None
    except Exception:
        # 第三方后端可能抛出多种格式异常；只在此边界将损坏哈希视为凭证失败。
        return False
