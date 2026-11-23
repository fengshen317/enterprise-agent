"""验证文件编码以及真实问答、审批、个人材料和下载之间的契约。"""

import asyncio
import csv
import json
from io import BytesIO, StringIO
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from openpyxl import load_workbook
from pydantic import SecretStr, ValidationError

from app.agents.provider import DeepSeekProvider
from app.capabilities.builtins.deliverables import CREATE_DELIVERABLE, DeliverableArgs, render_deliverable
from tests.auth_helpers import add_identity
from tests.test_chat_agent_integration import response
from tests.test_chat_integration import ControlledProvider, new_conversation, settled, submit
from tests.test_chat_integration import chat as chat

TABLE = {"title": "跟进表", "columns": ["事项", "数量"], "rows": [["核对,进度", 12], ["=A1", -2]]}


def test_report_and_csv_bytes() -> None:
    """验证中文正文、换行转义和公式样式字符串按文字导出。"""
    markdown = "# 周报\n\n已确认里程碑。"
    report = DeliverableArgs.model_validate({"format": "md", "title": "周报", "markdown": markdown})
    assert render_deliverable(report).decode() == markdown
    body = render_deliverable(DeliverableArgs.model_validate({"format": "csv", **TABLE}))
    assert list(csv.reader(StringIO(body.decode("utf-8-sig")))) == [
        TABLE["columns"],
        ["核对,进度", "12"],
        ["'=A1", "-2"],
    ]


def test_xlsx_keeps_long_identifier_and_filename() -> None:
    """保留超过 Excel 数字精度的编号和用户给定的扩展名。"""
    args = DeliverableArgs.model_validate({
        "format": "xlsx", "title": "明细.xlsx", "columns": ["编号", "有效"], "rows": [[123456789012345678, True]]
    })
    assert args.filename == "明细.xlsx"
    workbook = load_workbook(BytesIO(render_deliverable(args)))
    assert workbook.active["A2"].value == "123456789012345678"
    assert workbook.active["B2"].value is True
    workbook.close()


@pytest.mark.parametrize("change", [{"title": "../清单"}, {"rows": [["缺一列"]]}, {"format": "md"}])
def test_reject_invalid_input(change: dict[str, object]) -> None:
    """错误的标题、行宽或格式不会产生不完整文件。"""
    with pytest.raises(ValidationError):
        DeliverableArgs.model_validate({"format": "csv", **TABLE, **change})


@pytest.mark.integration
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_chat_file_delivery(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    """经真实问答图和审批 API 生成文件，验证内容、归属、历史结果和拒绝路径。"""
    from langchain_deepseek import ChatDeepSeek

    app, client, _ = chat
    runtime = app.state.chat_runtime
    runtime.settings = runtime.settings.model_copy(update={"deepseek_api_key": SecretStr("test-only")})
    runtime.provider = DeepSeekProvider(runtime.settings)

    def transport(request: httpx.Request) -> httpx.Response:
        """只替换上游 HTTP，校验模型实际收到的工具和本地执行结果。"""
        body = json.loads(request.content)
        assert CREATE_DELIVERABLE in {tool["function"]["name"] for tool in body["tools"]}
        results = [json.loads(row["content"]) for row in body["messages"] if row["role"] == "tool"]
        if not results:
            return response(name=CREATE_DELIVERABLE, arguments={"format": "xlsx", **TABLE})
        if decision == "reject":
            assert results[-1]["error_code"] == "APPROVAL_REJECTED"
            return response(content="已取消生成文件。")
        result = results[-1]["data"]
        return response(content=f"已生成[跟进表]({result['url']})。")

    def model(**kwargs: Any) -> ChatDeepSeek:
        """保留真实 SDK 的参数序列化及工具分派。"""
        return ChatDeepSeek(**kwargs, http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", model)
    conversation = await new_conversation(client)
    run_id = (await submit(client, conversation, "请生成一份可下载的 Excel 跟进表。"))["run"]["id"]
    async with asyncio.timeout(30):
        while True:
            snapshot = (await client.get(f"/api/runs/{run_id}")).json()
            assert snapshot["run"]["status"] in {"pending", "running"}, snapshot
            if approval := snapshot["approval"]:
                break
            await asyncio.sleep(0.04)
    assert approval["tool_name"] == CREATE_DELIVERABLE
    assert "跟进表.xlsx" in approval["args_preview"]["summary"]
    assert (await client.get("/api/materials")).json()["items"] == []
    decided = await client.post(f"/api/runs/{run_id}/approvals/{approval['id']}/decide", json={"decision": decision})
    assert decided.is_success
    finished = await settled(client, run_id)
    assert finished["run"]["status"] == "completed", finished
    items = (await client.get("/api/materials")).json()["items"]
    if decision == "reject":
        assert items == []
        return
    (file,) = items
    assert finished["message"]["attachments"][0]["id"] == file["id"]
    history = (await client.get(f"/api/conversations/{conversation}/messages")).json()
    assert history["items"][-1]["attachments"] == finished["message"]["attachments"]
    identifier = file["id"]
    assert file["source_conversation_id"] == conversation and file["filename"] == "跟进表.xlsx"
    assert "/materials/" + identifier in finished["message"]["content"]
    path = f"/api/materials/{identifier}"
    assert "核对,进度" in (await client.get(path)).json()["excerpt"]
    download = await client.get(path + "/file?download=true")
    assert download.status_code == 200 and download.headers["Content-Disposition"].startswith("attachment")
    book = load_workbook(BytesIO(download.content))
    assert book.active["B2"].value == 12 and book.active["B2"].data_type == "n"
    assert book.active["A3"].value == "=A1" and book.active["A3"].data_type == "s"
    book.close()
    trace = (await client.get(f"/api/runs/{run_id}/trace")).json()
    assert trace["approvals"][0]["status"] == "executed"
    call = next(item for item in trace["tool_calls"] if item["tool_name"] == CREATE_DELIVERABLE)
    assert call["status"] == "completed" and json.loads(call["result"])["id"] == identifier
    assert UUID(call["id"]) == UUID(identifier)
    workspace = (await client.get("/api/auth/me")).json()["workspaces"][0]["slug"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
        await add_identity(app, other, workspace=workspace, role="admin")
        assert (await other.get(path + "/file")).status_code == 404
