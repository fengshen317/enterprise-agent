"""验证附件格式、消息归属、重试幂等和视觉模型请求。"""

import json
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import pytest
from fastapi import FastAPI
from openpyxl import Workbook
from PIL import Image
from pydantic import SecretStr
from pypdf import PdfWriter
from sqlalchemy import delete

from app.agents.provider import DeepSeekProvider
from app.core.errors import ApplicationError
from app.models.conversation import Conversation, Message, Run
from app.schemas.chat import RunCreate
from app.services.chat_attachments import prepare_attachment
from app.services.tools import ToolExecutor
from tests.auth_helpers import add_identity
from tests.test_chat_agent_integration import response
from tests.test_chat_integration import ControlledProvider, new_conversation, settled
from tests.test_chat_integration import chat as chat


def sample_png() -> bytes:
    """生成不含私人资料的微型图片。"""
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "green").save(buffer, "PNG")
    return buffer.getvalue()


def test_chat_attachment_reads_bounded_document_formats() -> None:
    """验证中文文件名、Word 文本与没有文本层的 PDF。"""
    scope = uuid4()
    body = BytesIO()
    with ZipFile(body, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>附件验收</w:t></w:r></w:p></w:body></w:document>"
        ))
    doc = prepare_attachment(scope, r"C:\fakepath\测试.docx", body.getvalue())
    assert doc.view.filename == "测试.docx" and doc.view.text_available and doc.text == "附件验收"
    sheet_data = BytesIO()
    workbook = Workbook()
    workbook.active.append(["本期统计", 31])
    workbook.save(sheet_data)
    sheet = prepare_attachment(scope, "统计.xlsx", sheet_data.getvalue())
    assert sheet.view.text_available and "本期统计\t31" in sheet.text
    pdf = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    assert not prepare_attachment(scope, "扫描件.pdf", pdf.getvalue()).view.text_available
    limited = prepare_attachment(scope, "较长资料.txt", ("文字" * 7000).encode())
    assert limited.view.text_truncated and len(limited.text) == 12_000
    with pytest.raises(ApplicationError, match="附件内容无法读取"):
        prepare_attachment(scope, "伪装图片.png", b"<svg></svg>")


@pytest.mark.integration
async def test_chat_upload_archives_material_and_reuses_it_in_a_new_conversation(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """聊天材料跨本人对话可复用，删除来源对话仍保留原件。"""
    app, client, provider = chat
    conversation = await new_conversation(client)
    path = f"/api/conversations/{conversation}"
    fields = {"content": "请核对文件", "idempotency_key": "with-files"}
    bad = await client.post(f"{path}/runs/attachments", data=fields, files={"files": ("x.svg", b"<svg/>")})
    assert bad.status_code == 422
    assert (await client.get(f"{path}/messages")).json()["items"] == []
    original = "专用测试内容".encode()
    files = {"files": ("验收.txt", original, "text/plain")}
    saved = await client.post(f"{path}/runs/attachments", data=fields, files=files)
    assert saved.status_code == 202, saved.text
    run_id = saved.json()["run"]["id"]
    assert (await settled(client, run_id))["run"]["status"] == "completed"
    assert "专用测试内容" in provider.histories[-1][-1][1]
    history = (await client.get(f"{path}/messages")).json()["items"]
    assert history[0]["content"] == "请核对文件"
    (item,) = history[0]["attachments"]
    assert item["filename"] == "验收.txt" and item["text_available"]
    file_path = f"/attachments/{item['id']}/file"
    downloaded = await client.get(path + file_path)
    assert downloaded.content == original and downloaded.headers["content-disposition"].startswith("attachment;")

    duplicate = await client.post(f"{path}/runs/attachments", data=fields, files=files)
    assert duplicate.json()["run"]["id"] == run_id
    changed = await client.post(f"{path}/runs/attachments", data=fields, files={"files": ("验收.txt", b"other")})
    assert changed.status_code == 409
    other = await new_conversation(client)
    assert (await client.get(f"/api/conversations/{other}" + file_path)).status_code == 404
    reuse = {"content": "从原件再核对一次", "idempotency_key": "reuse", "attachment_ids": [item["id"]]}
    second = await client.post(f"/api/conversations/{other}/runs", json=reuse)
    assert second.status_code == 202
    await settled(client, second.json()["run"]["id"])
    assert "专用测试内容" in provider.histories[-1][-1][1]
    messages = (await client.get(f"/api/conversations/{other}/messages")).json()["items"]
    assert messages[0]["attachments"] == [item]
    assert (await client.get(f"/api/conversations/{other}" + file_path)).content == original
    async with app.state.database.session_factory.begin() as session:
        source = UUID(conversation)
        await session.execute(delete(Message).where(Message.conversation_id == source))
        await session.execute(delete(Run).where(Run.conversation_id == source))
        await session.execute(delete(Conversation).where(Conversation.id == source))
    (archived,) = (await client.get("/api/materials", params={"q": "专用测试"})).json()["items"]
    assert archived["id"] == item["id"] and archived["source_conversation_id"] is None


@pytest.mark.integration
async def test_image_is_sent_to_deepseek_flash_with_real_agent_graph(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """检查官方 Chat Completions 视觉载荷和浏览器图片权限。"""
    from langchain_deepseek import ChatDeepSeek

    app, client, _ = chat
    runtime = app.state.chat_runtime
    runtime.settings = runtime.settings.model_copy(update={
        "deepseek_model": "deepseek-v4-pro", "deepseek_api_key": SecretStr("fake-transport-only"),
    })
    runtime.provider = DeepSeekProvider(runtime.settings)
    requests: list[dict[str, Any]] = []
    initialize = ChatDeepSeek.__init__

    async def transport(request: httpx.Request) -> httpx.Response:
        """记录 SDK 请求并返回可解析的模拟 SSE。"""
        requests.append(json.loads(request.content))
        return response("图片已读取。")

    def configure(self: ChatDeepSeek, *args: Any, **kwargs: Any) -> None:
        """给真实模型 SDK 注入独立测试传输。"""
        kwargs["http_async_client"] = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        initialize(self, *args, **kwargs)

    monkeypatch.setattr(ChatDeepSeek, "__init__", configure)
    conversation = await new_conversation(client)
    image = sample_png()
    path = f"/api/conversations/{conversation}"
    saved = await client.post(f"{path}/runs/attachments", data={"idempotency_key": "image-only"},
                              files={"files": ("截图.png", image, "image/png")})
    assert saved.status_code == 202, saved.text
    run = saved.json()["run"]
    assert run["model"] == "deepseek-flash"
    terminal = await settled(client, run["id"])
    assert terminal["message"]["content"] == "图片已读取。", terminal
    (request,) = requests
    assert request["model"] == "deepseek-flash"
    assert request["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBORw0K")
    (item,) = (await client.get(f"{path}/messages")).json()["items"][0]["attachments"]
    browser = await client.get(f"{path}/attachments/{item['id']}/file")
    assert browser.headers["content-type"] == "image/png" and browser.content == image


@pytest.mark.integration
async def test_private_material_search_and_read_use_the_callers_workspace(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
) -> None:
    """直接归档可被后续 Agent 检索，其他成员即使是管理员也无法读取。"""
    app, client, _ = chat
    original = "项目代号为芒草，交付窗口周三。".encode()
    files = {"files": ("项目记录.txt", original, "text/plain")}
    uploaded = await client.post("/api/materials", files=files)
    assert uploaded.status_code == 201, uploaded.text
    (file,) = uploaded.json()
    assert file["source_conversation_id"] is None
    result = await client.post("/api/materials", files=files)
    assert result.json()[0]["id"] == file["id"]
    workspace = (await client.get("/api/auth/me")).json()["workspaces"][0]["slug"]
    path = f"/api/materials/{file['id']}"
    assert (await client.get(path + "/file")).content == original
    assert (await client.get("/api/materials", params={"q": "芒草"})).json()["items"][0]["excerpt"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        await add_identity(app, other, workspace=workspace, role="admin")
        assert (await other.get("/api/materials", params={"q": "芒草"})).json()["items"] == []
        assert (await other.get(path)).status_code == 404
        assert (await other.get(path + "/file")).status_code == 404
        unauthorized_chat = await new_conversation(other)
        refused = await other.post(f"/api/conversations/{unauthorized_chat}/runs", json={
            "content": "引用附件", "idempotency_key": "denied", "attachment_ids": [file["id"]],
        })
        assert refused.status_code == 404

    runtime = app.state.chat_runtime
    conversation = UUID(await new_conversation(client))
    run = await runtime.create_run(conversation, RunCreate(content="查找芒草", idempotency_key="materials"),
                                   app.state.test_scope)
    await runtime.store.begin_run(run.run.id)
    assembly = await runtime.assembler.assemble_for_run(run.run.id)
    executor = ToolExecutor(app.state.database.session_factory, runtime.settings, run.run.id,
                            assembly=assembly, registry=runtime.registry)
    search = json.loads(await executor.invoke("search_uploaded_materials", {"query": "芒草"}))
    (hit,) = search["data"]["items"]
    assert hit["id"] == file["id"] and hit["url"] == f"/materials/{file['id']}"
    read = json.loads(await executor.invoke("read_uploaded_material", {"material_id": hit["id"]}))
    assert read["data"]["text"] == original.decode()
