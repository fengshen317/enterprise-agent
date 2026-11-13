"""真实 PostgreSQL 下的知识库权限、文档状态与检索工具验收。"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agents.provider import ChatHistory
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.main import create_app
from app.models.identity import Workspace, WorkspaceMembership
from app.models.knowledge import KnowledgeDocument
from app.schemas.chat import RunCreate
from app.schemas.knowledge import CHUNKER_COMPONENT, KNOWLEDGE_TOOL
from app.services.ragflow import ChunkInfo, ChunkPage, DatasetInfo, DocumentFile, DocumentInfo, RetrievedChunk
from app.services.tools import ToolExecutor
from tests.auth_helpers import add_identity, cleanup_identities

pytestmark = pytest.mark.integration
BACKEND_DIR = Path(__file__).resolve().parents[1]


class NoopProvider:
    """应用启动所需的最小模型替身。"""

    def validate(self) -> None:
        """测试不调用真实模型。"""

    async def stream(self, history: ChatHistory, *, model: str | None = None) -> AsyncIterator[str]:
        """返回固定正文。

        Args:
            history: 对话历史。

        Yields:
            固定文本。
        """
        del model
        yield "测试回答"

    async def summarize(
        self, previous: str, history: ChatHistory, max_characters: int, *, model: str | None = None
    ) -> str:
        """返回固定摘要。

        Args:
            previous: 旧摘要。
            history: 新增历史。
            max_characters: 摘要预算。

        Returns:
            固定摘要。
        """
        del model
        return "摘要"


class FakeRagflow:
    """内存版 RAGFlow，验证应用编排而不发送真实网络请求。"""

    def __init__(self) -> None:
        """初始化空数据集。"""
        self.datasets: dict[str, dict[str, object]] = {}
        self.last_patch: dict[str, object] | None = None
        self.applied: list[str] = []
        self.parse_calls: list[tuple[str, list[str]]] = []
        self.last_retrieve: tuple[list[str], str, int, float] | None = None
        self.download_size_override: int | None = None

    @staticmethod
    def default_config() -> dict[str, object]:
        """返回与 1.0 实例结构一致的最小配置。

        Returns:
            parser_config。
        """
        return {
            CHUNKER_COMPONENT: {
                "chunk_token_size": 512,
                "delimiters": ["\n", "。", "；"],
                "overlapped_percent": 0.0,
                "parent_child": {"children_delimiter": "\n", "use_parent_child": False},
            },
            "Parser:HipSignsRhyme": {"pdf": {"parse_method": "DeepDOC"}},
        }

    async def create_dataset(
        self, name: str, parser_id: str, parse_type: int, embedding_model: str = "", language: str = ""
    ) -> DatasetInfo:
        """创建内存 dataset。

        Args:
            name: 名称。
            parser_id: 解析器。
            parse_type: 解析类型。
            embedding_model: embedding 模型。
            language: 语言。

        Returns:
            dataset 信息。
        """
        dataset_id = "ds-" + uuid4().hex
        config = self.default_config()
        self.datasets[dataset_id] = {"name": name, "parser_config": config, "docs": {}}
        return DatasetInfo(dataset_id=dataset_id, parser_config=config)

    async def update_dataset(
        self,
        dataset_id: str,
        parser_id: str,
        parse_type: int,
        parser_config: dict[str, object],
        embedding_model: str = "",
    ) -> None:
        """保存 dataset 配置。

        Args:
            dataset_id: 数据集标识。
            parser_id: 解析器。
            parse_type: 解析类型。
            parser_config: 配置。
            embedding_model: embedding 模型。
        """
        self.datasets[dataset_id]["parser_config"] = parser_config

    async def upload_document(self, dataset_id: str, filename: str, content: bytes, content_type: str) -> DocumentInfo:
        """创建内存文档。

        Args:
            dataset_id: 数据集标识。
            filename: 文件名。
            content: 内容。
            content_type: MIME。

        Returns:
            文档信息。
        """
        document_id = "rd-" + uuid4().hex
        config = self.datasets[dataset_id]["parser_config"]
        docs = self.datasets[dataset_id]["docs"]
        assert isinstance(docs, dict)
        docs[document_id] = {
            "name": filename,
            "parser_config": config,
            "content": content,
            "mime": content_type,
        }
        return DocumentInfo(
            document_id=document_id,
            name=filename,
            status="UNSTART",
            parser_config=dict(config) if isinstance(config, dict) else {},
        )

    async def patch_document_parser(
        self,
        dataset_id: str,
        document_id: str,
        parser_id: str,
        parse_type: int,
        parser_config: dict[str, object],
    ) -> None:
        """保存文档解析配置。

        Args:
            dataset_id: 数据集标识。
            document_id: 文档标识。
            parser_id: 解析器。
            parse_type: 解析类型。
            parser_config: 配置。
        """
        docs = self.datasets[dataset_id]["docs"]
        assert isinstance(docs, dict)
        docs[document_id]["parser_config"] = parser_config
        self.last_patch = parser_config

    async def apply_dataset_parser_config(self, dataset_id: str) -> None:
        """记录知识库配置批量应用。

        Args:
            dataset_id: 数据集标识。
        """
        self.applied.append(dataset_id)

    async def parse_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """记录解析请求。

        Args:
            dataset_id: 数据集标识。
            document_ids: 文档列表。
        """
        self.parse_calls.append((dataset_id, list(document_ids)))

    async def list_documents(self, dataset_id: str) -> list[DocumentInfo]:
        """返回全部已完成文档。

        Args:
            dataset_id: 数据集标识。

        Returns:
            文档状态列表。
        """
        docs = self.datasets[dataset_id]["docs"]
        assert isinstance(docs, dict)
        return [
            DocumentInfo(
                document_id=document_id,
                name=str(item["name"]),
                status="COMPLETED",
                chunk_count=3,
                token_count=100,
                parser_config=item["parser_config"] if isinstance(item["parser_config"], dict) else {},
                progress=1.0,
            )
            for document_id, item in docs.items()
            if isinstance(item, dict)
        ]

    async def download_document(self, dataset_id: str, document_id: str) -> DocumentFile:
        """返回上传时保存的原文件。

        Args:
            dataset_id: 数据集标识。
            document_id: 文档标识。

        Returns:
            流式文件。
        """
        docs = self.datasets[dataset_id]["docs"]
        assert isinstance(docs, dict)
        item = docs.get(document_id)
        if not isinstance(item, dict):
            raise ApplicationError("RAGFLOW_REJECTED", "文档不存在", 502)
        content = item.get("content")
        raw = content if isinstance(content, bytes) else b""
        length = self.download_size_override if self.download_size_override is not None else len(raw)

        async def blocks() -> AsyncIterator[bytes]:
            """输出原文件字节。"""
            yield raw

        return DocumentFile(
            content_type=str(item.get("mime") or "text/plain"),
            content_length=length,
            blocks=blocks(),
        )

    async def list_chunks(self, dataset_id: str, document_id: str, page: int, size: int) -> ChunkPage:
        """返回固定 30 个分块的分页结果。

        Args:
            dataset_id: 数据集标识。
            document_id: 文档标识。
            page: 页码。
            size: 每页条数。

        Returns:
            分块分页结果。
        """
        total = 30
        start = (page - 1) * size
        chunks = [
            ChunkInfo(content=f"chunk {index}", available=index != 5)
            for index in range(start, min(start + size, total))
        ]
        return ChunkPage(total=total, chunks=chunks)

    async def delete_documents(self, dataset_id: str, document_ids: list[str]) -> None:
        """删除内存文档。

        Args:
            dataset_id: 数据集标识。
            document_ids: 文档列表。
        """
        docs = self.datasets[dataset_id]["docs"]
        assert isinstance(docs, dict)
        for document_id in document_ids:
            docs.pop(document_id, None)

    async def delete_dataset(self, dataset_id: str) -> None:
        """删除内存数据集。

        Args:
            dataset_id: 数据集标识。
        """
        self.datasets.pop(dataset_id, None)

    async def retrieve(
        self,
        dataset_ids: list[str],
        question: str,
        top_k: int,
        threshold: float,
        document_ids: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """返回带来源的内存片段。

        Args:
            dataset_ids: 允许的数据集。
            question: 查询。
            top_k: 返回条数。
            threshold: 阈值。
            document_ids: 文档过滤。

        Returns:
            检索片段。
        """
        self.last_retrieve = (list(dataset_ids), question, top_k, threshold)
        chunks: list[RetrievedChunk] = []
        for dataset_id in dataset_ids:
            dataset = self.datasets.get(dataset_id)
            if dataset is None:
                continue
            docs = dataset["docs"]
            assert isinstance(docs, dict)
            for document_id, item in docs.items():
                if document_ids and document_id not in document_ids:
                    continue
                if not isinstance(item, dict):
                    continue
                chunks.append(
                    RetrievedChunk(
                        content="知识库内容：" + question,
                        dataset_id=dataset_id,
                        document_id=document_id,
                        document_name=str(item["name"]),
                        similarity=0.9,
                        positions=[1],
                    )
                )
        return chunks[:top_k]

    async def aclose(self) -> None:
        """测试替身无需释放资源。"""


@pytest.fixture
async def knowledge_app() -> AsyncIterator[tuple[FastAPI, AsyncClient, FakeRagflow]]:
    """在专用测试库启动知识库应用。

    Yields:
        应用、管理员客户端与 RAGFlow 替身。
    """
    source = Settings()
    if not source.test_database_name:
        pytest.skip("未配置独立测试库")
    target = source.test_sqlalchemy_url
    assert target.database and target.database.endswith("_test")
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "EA_DATABASE_NAME": target.database},
        check=True,
    )
    settings = source.model_copy(
        update={
            "database_name": target.database,
            "environment": "test",
            "task_scheduler_enabled": False,
            "schedule_enabled": False,
            "knowledge_enabled": True,
            "ragflow_api_key": "test-key",
        }
    )
    fake = FakeRagflow()
    app = create_app(settings, NoopProvider(), ragflow_client=fake)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            scope = await add_identity(app, client, role="admin")
            app.state.test_scope = scope
            yield app, client, fake
    await cleanup_identities(app)


async def workspace_slug(app: FastAPI, user_id: UUID) -> str:
    """读取测试用户所在空间的 slug。

    Args:
        app: 测试应用。
        user_id: 用户标识。

    Returns:
        空间 slug。
    """
    async with app.state.database.session_factory() as session:
        slug = await session.scalar(
            select(Workspace.slug)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(WorkspaceMembership.user_id == user_id)
        )
    assert isinstance(slug, str)
    return slug


async def create_kb(client: AsyncClient, name: str = "制度库", visibility: str = "workspace") -> dict[str, object]:
    """创建测试知识库。

    Args:
        client: 已登录客户端。
        name: 名称。
        visibility: 可见性。

    Returns:
        响应 JSON。
    """
    response = await client.post(
        "/api/knowledge-bases",
        json={"name": name, "visibility": visibility, "parser_settings": {"chunk_token_size": 128}},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def upload_document(
    client: AsyncClient, kb_id: str, filename: str = "制度.txt", content: bytes = b"EA-KB-TOKEN"
) -> dict[str, object]:
    """上传测试文档。

    Args:
        client: 已登录客户端。
        kb_id: 知识库标识。
        filename: 文件名。
        content: 内容。

    Returns:
        响应 JSON。
    """
    response = await client.post(
        f"/api/knowledge-bases/{kb_id}/documents",
        files={"file": (filename, content, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def executor_for(app: FastAPI, client: AsyncClient, scope: object) -> ToolExecutor:
    """创建绑定真实运行的工具执行器。

    Args:
        app: 测试应用。
        client: 已登录客户端。
        scope: 当前可信范围。

    Returns:
        工具执行器。
    """
    response = await client.post("/api/conversations", json={"title": "知识库测试"})
    assert response.status_code == 201
    conversation_id = UUID(response.json()["id"])
    runtime = app.state.chat_runtime
    snapshot = await runtime.store.create_run(
        conversation_id,
        RunCreate(content="检索知识库", idempotency_key=uuid4().hex),
        "test",
        scope,
    )
    await runtime.store.begin_run(snapshot.run.id)
    return ToolExecutor(
        app.state.database.session_factory,
        runtime.settings,
        snapshot.run.id,
        app.state.knowledge_service,
    )


async def test_knowledge_permissions_and_private_visibility(
    knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow],
) -> None:
    """空间管理员创建空间库，成员只能建私有库，管理员不能读他人私有库。"""
    app, admin, _ = knowledge_app
    admin_scope = app.state.test_scope
    workspace = await workspace_slug(app, admin_scope.user_id)
    workspace_kb = await create_kb(admin)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member_client:
        await add_identity(app, member_client, workspace=workspace, role="member")
        listed = await member_client.get("/api/knowledge-bases")
        assert [row["id"] for row in listed.json()] == [workspace_kb["id"]]
        forbidden = await member_client.post(
            "/api/knowledge-bases", json={"name": "成员空间库", "visibility": "workspace"}
        )
        assert forbidden.status_code == 403
        private = await create_kb(member_client, name="成员私有库", visibility="private")

        admin_list = await admin.get("/api/knowledge-bases")
        admin_ids = [row["id"] for row in admin_list.json()]
        assert workspace_kb["id"] in admin_ids
        assert private["id"] not in admin_ids
        assert (await admin.get(f"/api/knowledge-bases/{private['id']}")).status_code == 404
        assert (await member_client.get(f"/api/knowledge-bases/{private['id']}")).status_code == 200
        assert (
            await member_client.post(
                f"/api/knowledge-bases/{workspace_kb['id']}", json={"name": "篡改", "visibility": "workspace"}
            )
        ).status_code == 403
        assert (
            await member_client.post(
                f"/api/knowledge-bases/{workspace_kb['id']}/documents",
                files={"file": ("x.txt", b"x", "text/plain")},
            )
        ).status_code == 403


async def test_upload_parse_and_tool_retrieval(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """上传后 PATCH 配置并解析，工具只返回应用侧来源。"""
    app, client, _ = knowledge_app
    scope = app.state.test_scope
    kb = await create_kb(client)
    uploaded = await upload_document(client, str(kb["id"]))
    assert uploaded["status"] == "parsing"

    documents = await client.get(f"/api/knowledge-bases/{kb['id']}/documents")
    assert documents.status_code == 200
    rows = documents.json()
    assert rows[0]["status"] == "ready"
    assert rows[0]["chunk_count"] == 3

    executor = await executor_for(app, client, scope)
    raw = await executor.invoke(KNOWLEDGE_TOOL, {"query": "年假制度", "top_k": 5})
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["data"]["chunks"][0]["document_id"] == rows[0]["id"]
    assert result["data"]["chunks"][0]["document_name"] == "制度.txt"
    assert "ds-" not in raw and "rd-" not in raw
    records = (await client.get(f"/api/runs/{executor.run_id}/tool-calls")).json()
    assert records[0]["tool_name"] == KNOWLEDGE_TOOL
    assert records[0]["days"] is None
    assert "年假制度" in records[0]["result"]
    assert "知识库内容" not in records[0]["result"]


async def test_reparse_uses_current_kb_parser_settings(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """知识库配置更新后，重新解析使用当前配置而不是旧快照。"""
    app, client, fake = knowledge_app
    kb = await create_kb(client)
    uploaded = await upload_document(client, str(kb["id"]))
    updated = await client.post(
        f"/api/knowledge-bases/{kb['id']}",
        json={
            "name": "制度库",
            "description": "",
            "visibility": "workspace",
            "parser_settings": {
                "chunk_token_size": 256,
                "delimiters": ["\n"],
                "overlapped_percent": 0,
                "use_parent_child": False,
                "parent_child_delimiter": "\n",
            },
        },
    )
    assert updated.status_code == 200, updated.text
    reparsed = await client.post(f"/api/knowledge-documents/{uploaded['id']}/parse", json={})
    assert reparsed.status_code == 200, reparsed.text
    assert fake.last_patch is not None
    component = fake.last_patch[CHUNKER_COMPONENT]
    assert isinstance(component, dict)
    assert component["chunk_token_size"] == 256


async def test_other_workspace_cannot_retrieve(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """其他空间的运行无法检索当前空间知识库。"""
    app, admin, _ = knowledge_app
    await create_kb(admin)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        other_scope = await add_identity(app, other, role="admin")
        executor = await executor_for(app, other, other_scope)
        result = json.loads(await executor.invoke(KNOWLEDGE_TOOL, {"query": "制度"}))
        assert result["ok"] is False
        assert result["error_code"] == "KNOWLEDGE_ACCESS_DENIED"


async def test_document_file_download_and_permissions(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """原文件下载走鉴权代理，空间成员可读、异空间与私有库非创建者不可读。"""
    app, admin, _ = knowledge_app
    scope = app.state.test_scope
    workspace = await workspace_slug(app, scope.user_id)
    kb = await create_kb(admin)
    uploaded = await upload_document(admin, str(kb["id"]), content=b"hello kb")
    response = await admin.get(f"/api/knowledge-documents/{uploaded['id']}/file")
    assert response.status_code == 200
    assert response.content == b"hello kb"
    assert "inline" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        await add_identity(app, member, workspace=workspace, role="member")
        assert (await member.get(f"/api/knowledge-documents/{uploaded['id']}/file")).status_code == 200
        private = await create_kb(member, name="成员私有库", visibility="private")
        private_doc = await upload_document(member, str(private["id"]))
        assert (await member.get(f"/api/knowledge-documents/{private_doc['id']}/file")).status_code == 200

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await add_identity(app, other, role="admin")
        assert (await other.get(f"/api/knowledge-documents/{uploaded['id']}/file")).status_code == 404
        assert (await admin.get(f"/api/knowledge-documents/{private_doc['id']}/file")).status_code == 404


async def test_download_oversize_is_rejected(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """超过原文件大小上限时返回 413，不开始传输。"""
    app, client, fake = knowledge_app
    kb = await create_kb(client)
    uploaded = await upload_document(client, str(kb["id"]))
    fake.download_size_override = 10**9
    response = await client.get(f"/api/knowledge-documents/{uploaded['id']}/file")
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "KNOWLEDGE_FILE_TOO_LARGE"


async def test_document_chunks_pagination(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """分块列表分页返回且不暴露 RAGFlow chunk ID。"""
    app, client, _ = knowledge_app
    kb = await create_kb(client)
    uploaded = await upload_document(client, str(kb["id"]))
    first = await client.get(f"/api/knowledge-documents/{uploaded['id']}/chunks?offset=0&limit=20")
    assert first.status_code == 200
    page = first.json()
    assert page["total"] == 30
    assert len(page["chunks"]) == 20
    assert page["chunks"][0]["content"] == "chunk 0"
    assert "chunk_id" not in first.text
    second = await client.get(f"/api/knowledge-documents/{uploaded['id']}/chunks?offset=20&limit=20")
    assert second.json()["chunks"][0]["content"] == "chunk 20"
    assert len(second.json()["chunks"]) == 10


async def test_retrieval_test_returns_similarity_and_sources(
    knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow],
) -> None:
    """检索测试返回完整来源与相似度，且不泄漏 RAGFlow ID。"""
    app, client, fake = knowledge_app
    kb = await create_kb(client)
    uploaded = await upload_document(client, str(kb["id"]))
    response = await client.post(
        f"/api/knowledge-bases/{kb['id']}/retrieval-test",
        json={"query": "年假制度", "top_k": 3, "threshold": 0.55},
    )
    assert response.status_code == 200, response.text
    chunks = response.json()["chunks"]
    assert chunks[0]["document_id"] == uploaded["id"]
    assert chunks[0]["similarity"] == 0.9
    assert chunks[0]["positions"] == [1]
    assert "ds-" not in response.text and "rd-" not in response.text
    dataset_id = next(iter(fake.datasets))
    assert fake.last_retrieve == ([dataset_id], "年假制度", 3, 0.55)


async def test_reparse_all_documents(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """批量重解析先同步知识库配置，再一次性提交全部远程文档。"""
    app, client, fake = knowledge_app
    kb = await create_kb(client)
    uploaded = await upload_document(client, str(kb["id"]))
    dataset_id = next(iter(fake.datasets))
    remote_id = next(iter(fake.datasets[dataset_id]["docs"]))
    fake.applied.clear()
    fake.parse_calls.clear()
    response = await client.post(f"/api/knowledge-bases/{kb['id']}/reparse-documents")
    assert response.status_code == 200, response.text
    assert fake.applied == [dataset_id]
    assert fake.parse_calls == [(dataset_id, [remote_id])]
    assert response.json()[0]["status"] == "ready"
    async with app.state.database.session_factory() as session:
        doc = await session.get(KnowledgeDocument, UUID(uploaded["id"]))
        assert doc is not None
        component = doc.parse_config[CHUNKER_COMPONENT]
        assert component["chunk_token_size"] == 128


async def test_list_bases_returns_statistics(knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow]) -> None:
    """列表与详情返回实时文档数和 chunk 数。"""
    app, client, _ = knowledge_app
    kb = await create_kb(client)
    await upload_document(client, str(kb["id"]))
    await client.get(f"/api/knowledge-bases/{kb['id']}/documents")
    listed = await client.get("/api/knowledge-bases")
    row = next(item for item in listed.json() if item["id"] == kb["id"])
    assert row["document_count"] == 1
    assert row["chunk_count"] == 3
    detail = await client.get(f"/api/knowledge-bases/{kb['id']}")
    assert detail.json()["document_count"] == 1
    assert detail.json()["chunk_count"] == 3


async def test_upload_validation_delete_and_dataset_cleanup(
    knowledge_app: tuple[FastAPI, AsyncClient, FakeRagflow],
) -> None:
    """不支持的类型被拒绝，删除文档和知识库会同步清理替代服务。"""
    app, client, fake = knowledge_app
    kb = await create_kb(client)
    bad = await client.post(
        f"/api/knowledge-bases/{kb['id']}/documents",
        files={"file": ("bad.exe", b"x", "application/octet-stream")},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "KNOWLEDGE_FILE_TYPE"

    uploaded = await upload_document(client, str(kb["id"]))
    deleted = await client.post(f"/api/knowledge-documents/{uploaded['id']}/delete")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/knowledge-bases/{kb['id']}/documents")).json() == []

    removed = await client.post(f"/api/knowledge-bases/{kb['id']}/delete")
    assert removed.status_code == 204
    assert fake.datasets == {}
    assert (await client.get("/api/knowledge-bases")).json() == []
