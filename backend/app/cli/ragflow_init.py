"""初始化内置 RAGFlow 的知识库服务账号与 API Key。"""

import asyncio
import base64
import os
import secrets
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from app.core.config import MANAGED_RAGFLOW_URL, Settings

ADMIN_STATE = Path("/var/lib/ea-ragflow-admin")
API_KEY_FILE = Path("/var/lib/ea-ragflow-api/api-key")
ADMIN_URL = "http://ragflow:9381/api/v1/admin"
ADMIN_EMAIL = "admin@ragflow.io"
SERVICE_EMAIL = "enterprise-agent-knowledge@service.invalid"
API_UID = 10001
READY_TIMEOUT = 180
REQUEST_TIMEOUT = 10
RETRY_INTERVAL = 2
RAGFLOW_NOT_FOUND = 404
RAGFLOW_INVALID_KEY_CODES = {401, 403, 102, 108, 109}


class BootstrapError(Exception):
    """表示可安全写入初始化日志的错误。"""


def encrypt_password(public_key: rsa.RSAPublicKey, password: str) -> str:
    """按 RAGFlow Go 认证协议包装密码。

    Args:
        public_key: 镜像携带的 RSA 公钥。
        password: 部署生成的内部凭据。

    Returns:
        可放入认证请求的密文。
    """
    encrypted = public_key.encrypt(base64.b64encode(password.encode()), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def save_api_key(key: str) -> None:
    """原子交付只允许 API 运行账号读取的密钥。"""
    if sys.platform == "win32":
        raise BootstrapError("请在 Linux Compose 容器中初始化 RAGFlow")
    with NamedTemporaryFile(mode="w", dir=API_KEY_FILE.parent, delete=False, encoding="utf-8") as file:
        temp = Path(file.name)
        try:
            file.write(key)
            file.flush()
            os.fsync(file.fileno())
            os.chown(temp, API_UID, API_UID)
            temp.chmod(0o400)
            temp.replace(API_KEY_FILE)
        finally:
            temp.unlink(missing_ok=True)


def read_saved_key() -> str:
    """读取持久化 Key，首次安装时返回空值。"""
    try:
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def internal_passwords() -> tuple[str, str]:
    """在重试间保留同一服务账号密码并包装上游登录凭据。"""
    public_key = load_pem_public_key((ADMIN_STATE / "public.pem").read_bytes())
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise BootstrapError("RAGFlow RSA 公钥类型不匹配")
    password_file = ADMIN_STATE / "service-password"
    if not password_file.exists():
        with password_file.open("x", encoding="utf-8") as file:
            password_file.chmod(0o600)
            file.write(secrets.token_urlsafe(32))
    service_password = password_file.read_text(encoding="utf-8").strip()
    admin_password = (ADMIN_STATE / "admin-password").read_text(encoding="utf-8").strip()
    return encrypt_password(public_key, admin_password), encrypt_password(public_key, service_password)


def response_data(response: httpx.Response, stage: str) -> dict[str, Any]:
    """校验响应封装并隐藏包含凭据的上游详情。"""
    try:
        data = response.json()
    except ValueError as exc:
        raise BootstrapError(f"{stage}返回了无效响应") from exc
    if not isinstance(data, dict) or not isinstance(data.get("code"), int):
        raise BootstrapError(f"{stage}返回了无效响应")
    return data


async def login(client: httpx.AsyncClient, url: str, email: str, password: str) -> str:
    """申请初始化过程使用的 RAGFlow 会话。"""
    response = await client.post(url, json={"email": email, "password": password})
    data = response_data(response, "登录")
    token: str | None = response.headers.get("authorization")
    if response.is_error or data["code"] != 0 or not token:
        raise BootstrapError("内部账号认证失败，请核对已有 RAGFlow 凭据")
    return token


async def validate_key(client: httpx.AsyncClient, key: str) -> bool:
    """实测 Key 能否读取服务账号的数据集。"""
    response = await client.get(
        "/api/v1/datasets", params={"page_size": 1}, headers={"Authorization": f"Bearer {key}"}
    )
    data = response_data(response, "密钥校验")
    if response.status_code in (401, 403) or data["code"] in RAGFLOW_INVALID_KEY_CODES:
        return False
    if response.is_error or data["code"] != 0:
        raise BootstrapError("RAGFlow 暂时无法校验 API Key")
    return True


async def wait_ready(client: httpx.AsyncClient) -> None:
    """等待 RAGFlow 数据面可用，兼容首次冷启动。"""
    try:
        async with asyncio.timeout(READY_TIMEOUT):
            while True:
                try:
                    response = await client.get("/api/v1/system/healthz")
                    if response.is_success and response.json().get("status") == "ok":
                        return
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
                await asyncio.sleep(RETRY_INTERVAL)
    except TimeoutError as exc:
        raise BootstrapError("等待 RAGFlow 健康检查超时") from exc


async def provision(settings: Settings) -> None:
    """为内置实例生成凭据，沿用仍有效的外部或已签发密钥。"""
    if (
        not settings.knowledge_enabled
        or settings.ragflow_api_key.get_secret_value().strip()
        or settings.ragflow_base_url.rstrip("/") != MANAGED_RAGFLOW_URL
    ):
        print("RAGFlow 使用现有接入配置，跳过账号初始化")
        return

    async with httpx.AsyncClient(base_url=MANAGED_RAGFLOW_URL, timeout=REQUEST_TIMEOUT, trust_env=False) as client:
        await wait_ready(client)
        saved_key = await asyncio.to_thread(read_saved_key)
        if saved_key and await validate_key(client, saved_key):
            print("RAGFlow 已复用服务账号密钥")
            return

        admin_password, service_password = await asyncio.to_thread(internal_passwords)
        admin_token = await login(client, f"{ADMIN_URL}/login", ADMIN_EMAIL, admin_password)
        headers = {"Authorization": admin_token}
        response = await client.get(f"{ADMIN_URL}/users/{quote(SERVICE_EMAIL, safe='')}", headers=headers)
        user = response_data(response, "账号检查")
        if user["code"] == RAGFLOW_NOT_FOUND:
            response = await client.post(
                f"{ADMIN_URL}/users",
                headers=headers,
                json={"username": SERVICE_EMAIL, "password": service_password, "role": "user"},
            )
            if response.is_error or response_data(response, "账号创建")["code"] != 0:
                raise BootstrapError("创建 RAGFlow 服务账号失败")
        elif response.is_error or user["code"] != 0:
            raise BootstrapError("检查 RAGFlow 服务账号失败")

        service_token = await login(client, "/api/v1/auth/login", SERVICE_EMAIL, service_password)
        service_headers = {"Authorization": service_token}
        existing = await client.get("/api/v1/system/tokens", headers=service_headers)
        existing_data = response_data(existing, "密钥查询")
        if existing.is_error or existing_data["code"] != 0:
            raise BootstrapError("查询 RAGFlow 服务账号密钥失败")
        records = existing_data.get("data")
        if isinstance(records, list):
            for record in records:
                key = record.get("token") if isinstance(record, dict) else None
                if isinstance(key, str) and key.strip() and await validate_key(client, key):
                    await asyncio.to_thread(save_api_key, key)
                    print("RAGFlow 已恢复服务账号密钥")
                    return
        token_response = await client.post("/api/v1/system/tokens", json={}, headers=service_headers)
        token_data = response_data(token_response, "密钥签发")
        key_info = token_data.get("data")
        key = key_info.get("token") if isinstance(key_info, dict) else None
        if token_response.is_error or token_data["code"] != 0 or not isinstance(key, str) or not key.strip():
            raise BootstrapError(f"签发 RAGFlow API Key 失败（响应码 {token_data['code']}）")
        if not await validate_key(client, key):
            raise BootstrapError("新签发的 RAGFlow API Key 未通过验证")
        await asyncio.to_thread(save_api_key, key)
        print("RAGFlow 服务账号和 API Key 已就绪")


def main() -> int:
    """执行部署任务并输出可公开的故障阶段。"""
    os.umask(0o077)
    try:
        asyncio.run(provision(Settings()))
    except BootstrapError as exc:
        print(f"RAGFlow 初始化失败：{exc}")
        return 1
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"RAGFlow 初始化失败：请检查内网连接和密钥文件（{type(exc).__name__}）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
