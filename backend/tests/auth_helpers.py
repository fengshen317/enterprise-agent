"""通过真实账号哈希与登录接口建立测试身份，不绕过鉴权依赖。"""

from uuid import uuid4

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core.security import COOKIE_NAME
from app.models.agent import AgentProfile, WorkspaceToolPolicy
from app.models.conversation import Conversation, Message, Run
from app.models.identity import AuthSession, User, Workspace, WorkspaceMembership
from app.models.knowledge import KnowledgeBase, KnowledgeDocument
from app.models.schedule import ScheduledTask
from app.models.skill import Skill
from app.schemas.auth import ResourceScope
from app.services.accounts import AccountManager

TEST_PASSWORD = "test-only-password-123!"
ORIGIN = "http://127.0.0.1:8686"


async def add_identity(
    app: FastAPI, client: AsyncClient, workspace: str | None = None, role: str = "member"
) -> ResourceScope:
    """创建真实测试身份并调用 HTTP 登录。

    Args:
        app: 测试应用。
        client: 独立 Cookie 客户端。
        workspace: 加入已有空间，否则新建空间。
        role: 加入空间时的角色。

    Returns:
        供内部测试使用的已创建身份范围。
    """
    username = "qa_" + uuid4().hex
    slug = workspace or "qa-" + uuid4().hex
    user, space, _ = await AccountManager(app.state.database.session_factory).create(
        username, TEST_PASSWORD, "测试用户", slug=slug, initialize=workspace is None, role=role
    )
    if not hasattr(app.state, "test_users"):
        app.state.test_users, app.state.test_spaces = [], []
    app.state.test_users.append(user)
    if workspace is None:
        app.state.test_spaces.append(space)
    client.headers["Origin"] = ORIGIN
    response = await client.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    client.headers["X-Workspace-ID"] = str(space)
    return ResourceScope(user, space)


def copied_headers(client: AsyncClient) -> dict[str, str]:
    """把测试凭证显式传给同服务不同测试域名的 TCP 客户端。

    Args:
        client: 已登录测试客户端。

    Returns:
        请求头，不打印其内容。
    """
    return {**dict(client.headers), "Cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}"}


async def cleanup_identities(app: FastAPI) -> None:
    """只清理本夹具记录的账号和空间，不依赖宽泛名称匹配。

    Args:
        app: 持有已创建 ID 的测试应用。
    """
    users, spaces = app.state.test_users, app.state.test_spaces
    async with app.state.database.session_factory.begin() as session:
        await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.workspace_id.in_(spaces)))
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.workspace_id.in_(spaces)))
        await session.execute(delete(ScheduledTask).where(ScheduledTask.workspace_id.in_(spaces)))
        await session.execute(delete(AgentProfile).where(AgentProfile.workspace_id.in_(spaces)))
        await session.execute(delete(Skill).where(Skill.workspace_id.in_(spaces)))
        await session.execute(delete(WorkspaceToolPolicy).where(WorkspaceToolPolicy.workspace_id.in_(spaces)))
        conversations = select(Conversation.id).where(Conversation.workspace_id.in_(spaces))
        await session.execute(delete(Message).where(Message.conversation_id.in_(conversations)))
        await session.execute(delete(Run).where(Run.conversation_id.in_(conversations)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversations)))
        await session.execute(delete(AuthSession).where(AuthSession.user_id.in_(users)))
        await session.execute(delete(WorkspaceMembership).where(WorkspaceMembership.user_id.in_(users)))
        await session.execute(delete(Workspace).where(Workspace.id.in_(spaces)))
        await session.execute(delete(User).where(User.id.in_(users)))
