"""仅在受信运行上下文中解开当前成员的连接器凭证。"""

import json
import re

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.connector import Connector, PersonalConnectorBinding
from app.schemas.auth import ResourceScope
from app.schemas.connectors import PersonalAccountView, PersonalField
from app.services.auth import require_membership

_MAX_CREDENTIAL_LENGTH = 4096
FIELDS = {
    "qq_mail": (
        PersonalField(key="email", label="QQ 邮箱地址"),
        PersonalField(key="auth_code", label="IMAP 授权码", secret=True),
    ),
    "ima": (
        PersonalField(key="client_id", label="Client ID"),
        PersonalField(key="api_key", label="API Key", secret=True),
    ),
    "tencent_news": (PersonalField(key="api_key", label="腾讯新闻 API Key", secret=True),),
}
GUIDES = {
    "qq_mail": "https://hiflow.tencent.com/docs/applications/qq-mail/",
    "ima": "https://ima.qq.com/agent-interface",
    "tencent_news": "https://news.qq.com/exchange?scene=appkey",
}


def _cipher(settings: Settings) -> Fernet:
    """获取部署密钥，避免把无法恢复的临时密钥用于持久化。"""
    try:
        return Fernet(settings.connector_credential_key.get_secret_value().encode())
    except (TypeError, ValueError) as exc:
        raise ApplicationError("CONNECTOR_KEY_UNAVAILABLE", "请管理员配置个人连接器加密密钥", 503) from exc


async def account_status(session: AsyncSession, scope: ResourceScope, key: str) -> PersonalAccountView:
    """只返回当前成员的绑定状态与填写说明。"""
    bound = await session.scalar(
        select(PersonalConnectorBinding.connector_key).filter_by(
            workspace_id=scope.workspace_id, user_id=scope.user_id, connector_key=key
        )
    )
    return PersonalAccountView(bound=bound is not None, fields=list(FIELDS[key]), guide_url=GUIDES[key])


async def save_credentials(
    session: AsyncSession, settings: Settings, scope: ResourceScope, key: str, fields: dict[str, SecretStr]
) -> None:
    """原子替换本人在该空间的整组凭证，避免混用新旧账号字段。"""
    await require_membership(session, scope, write=True)
    if key not in FIELDS or not await session.scalar(select(Connector.enabled).where(Connector.key == key)):
        raise ApplicationError("CONNECTOR_DISABLED", "请先由管理员安装并启用连接器", 409)
    values = {name: secret.get_secret_value().strip() for name, secret in fields.items()}
    if set(values) != {field.key for field in FIELDS[key]} or any(
        not value or len(value) > _MAX_CREDENTIAL_LENGTH or any(ch in value for ch in "\r\n\0")
        for value in values.values()
    ):
        raise ApplicationError("INVALID_CREDENTIALS", "请完整填写该连接器所需凭证", 422)
    if key == "qq_mail" and not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", values["email"]):
        raise ApplicationError("INVALID_CREDENTIALS", "请填写有效的邮箱地址", 422)
    ciphertext = _cipher(settings).encrypt(json.dumps(values).encode()).decode()
    statement = insert(PersonalConnectorBinding).values(
        workspace_id=scope.workspace_id, user_id=scope.user_id, connector_key=key, ciphertext=ciphertext
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["workspace_id", "user_id", "connector_key"], set_={"ciphertext": ciphertext}
        )
    )


async def remove_credentials(session: AsyncSession, scope: ResourceScope, key: str) -> None:
    """停用连接器后仍允许成员解绑自己的账号。"""
    await session.execute(
        delete(PersonalConnectorBinding).filter_by(
            workspace_id=scope.workspace_id, user_id=scope.user_id, connector_key=key
        )
    )


async def read_credentials(session: AsyncSession, settings: Settings, scope: ResourceScope, key: str) -> dict[str, str]:
    """执行前复核启用与绑定，覆盖已装配的历史 Run。"""
    statement = (
        select(PersonalConnectorBinding.ciphertext)
        .join(Connector, Connector.key == PersonalConnectorBinding.connector_key)
        .where(
            Connector.enabled.is_(True),
            Connector.key == key,
            PersonalConnectorBinding.workspace_id == scope.workspace_id,
            PersonalConnectorBinding.user_id == scope.user_id,
        )
    )
    ciphertext = await session.scalar(statement)
    if ciphertext is None:
        raise ApplicationError("PERSONAL_CONNECTOR_UNBOUND", "请在当前工作空间绑定个人账号", 409)
    try:
        decoded: dict[str, str] = json.loads(_cipher(settings).decrypt(ciphertext.encode()))
    except InvalidToken as exc:
        raise ApplicationError("PERSONAL_CREDENTIAL_INVALID", "凭证无法读取，请重新绑定账号", 409) from exc
    return decoded
