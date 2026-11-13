"""应用入口与生命周期。"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.agents.provider import DeepSeekProvider, StreamProvider
from app.agents.task_provider import DeepSeekTaskProvider, TaskProvider
from app.api.admin import router as admin_router
from app.api.agents import router as agent_router
from app.api.auth import router as auth_router
from app.api.channels import router as channel_router
from app.api.chat import router as chat_router
from app.api.connectors import router as connector_router
from app.api.health import router
from app.api.knowledge import router as knowledge_router
from app.api.schedules import router as schedule_router
from app.api.skills import router as skill_router
from app.api.tasks import router as task_router
from app.api.tools import router as tool_router
from app.capabilities.registry import build_default_registry
from app.channels.crypto import ChannelCrypto
from app.channels.ilink import ILinkClient
from app.channels.repository import ChannelStore
from app.channels.runtime import ChannelRuntime
from app.channels.service import ChannelService
from app.connectors.registry import ConnectorRegistry
from app.core.config import Settings
from app.core.database import Database
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.services.agents import AgentService
from app.services.approvals import ApprovalService
from app.services.auth import AuthService
from app.services.chat_runtime import ChatRuntime
from app.services.config_service import ConfigService
from app.services.knowledge import KnowledgeService
from app.services.ragflow import RagflowClient, RagflowClientProtocol
from app.services.schedule_runtime import ScheduleRuntime
from app.services.schedule_store import ScheduleStore
from app.services.skills import SkillService
from app.services.task_runtime import TaskRuntime

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider: StreamProvider | None = None,
    task_provider: TaskProvider | None = None,
    ragflow_client: RagflowClientProtocol | None = None,
) -> FastAPI:
    """创建可注入配置的应用。

    Args:
        settings: 测试或运行配置，默认从环境读取。
        provider: 测试可注入模型替身；默认使用 DeepSeek。
        task_provider: 独立的计划和步骤模型，测试不发送真实请求。
        ragflow_client: 测试可注入的知识库客户端；默认使用真实 RAGFlow。

    Returns:
        FastAPI 应用实例。
    """
    config = settings or Settings()
    configure_logging(config.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """按进程生命周期创建与释放连接池。

        Args:
            app: 当前应用。

        Yields:
            应用可提供服务的生命周期。
        """
        database = Database(config)
        app.state.database = database
        app.state.auth_service = AuthService(database.session_factory, config)
        # 集成配置服务：从 DB 加载覆盖值并同步到 Settings，支持前端管理。
        config_service = ConfigService(database.session_factory, config)
        try:
            await config_service.load()
        except Exception:
            logger.warning("config_load_failed", extra={"reason": "DB 不可用或表未创建"})
        app.state.config_service = config_service
        ragflow = ragflow_client
        if ragflow is None and config.knowledge_enabled and config.ragflow_api_key.get_secret_value().strip():
            ragflow = RagflowClient(config)
        knowledge_service = (
            KnowledgeService(database.session_factory, config, ragflow)
            if config.knowledge_enabled and ragflow is not None
            else None
        )
        app.state.knowledge_service = knowledge_service
        schedule_store = ScheduleStore(database.session_factory, config.schedule_timezone)
        registry = build_default_registry(config, knowledge_service, schedule_store)
        # 连接器注册表：启动时加载已启用的连接器，工具注入 registry。
        connector_registry = ConnectorRegistry(database.session_factory, registry, config)
        try:
            await connector_registry.load()
        except Exception:
            logger.warning("connector_registry_load_failed")
        app.state.connector_registry = connector_registry
        approval_service = ApprovalService(database.session_factory, config)
        app.state.agent_service = AgentService(database.session_factory, config, registry)
        app.state.skill_service = SkillService(database.session_factory, config, registry)
        app.state.approval_service = approval_service
        runtime = ChatRuntime(
            database,
            config,
            provider or DeepSeekProvider(config),
            knowledge_service,
            registry,
            schedule_store=schedule_store,
            approval_service=approval_service,
        )
        app.state.chat_runtime = runtime
        task_runtime = TaskRuntime(database, config, task_provider or DeepSeekTaskProvider(config))
        app.state.task_runtime = task_runtime
        await task_runtime.start()
        schedule_runtime = ScheduleRuntime(database, config, runtime, schedule_store)
        app.state.schedule_runtime = schedule_runtime
        await schedule_runtime.start()
        channel_crypto = ChannelCrypto(config)
        ilink = ILinkClient(config)
        channel_store = ChannelStore(database.session_factory, channel_crypto)
        app.state.channel_service = ChannelService(channel_store, ilink, channel_crypto, config)
        channel_runtime = ChannelRuntime(
            database,
            config,
            channel_crypto,
            ilink,
            channel_store,
            runtime,
            approval_service,
            app.state.agent_service,
        )
        app.state.channel_runtime = channel_runtime
        runtime.on_run_finished.append(channel_runtime.on_run_finished)
        approval_service.on_requested.append(channel_runtime.on_approval)
        await channel_runtime.start()
        try:
            yield
        finally:
            try:
                await channel_runtime.close()
                await schedule_runtime.close()
                await task_runtime.close()
                await runtime.close()
                await connector_registry.close()
                await ilink.aclose()
                if ragflow is not None:
                    await ragflow.aclose()
            finally:
                await database.close()

    app = FastAPI(title="企业 Agent 工作台 API", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID", "X-CSRF-Token", "X-Workspace-ID"],
        allow_credentials=True,
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """生成可信关联标识，避免将外部输入直接写入日志。

        Args:
            request: 当前请求。
            call_next: 下游处理器。

        Returns:
            附有请求标识的响应。
        """
        request.state.request_id = uuid4().hex
        started = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        finally:
            logger.info(
                "http_request",
                extra={
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )

    register_error_handlers(app)
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(chat_router)
    app.include_router(connector_router)
    app.include_router(task_router)
    app.include_router(schedule_router)
    app.include_router(channel_router)
    app.include_router(knowledge_router)
    app.include_router(agent_router)
    app.include_router(skill_router)
    app.include_router(tool_router)
    return app


app = create_app()
