"""可选验收，使用真实模型、Docker 与独立 PostgreSQL。"""

import asyncio
import os
from io import BytesIO
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]

from app.agents.provider import DeepSeekProvider
from app.services.report_sandbox import ReportSandboxError, run_report_sandbox
from tests.test_chat_integration import ControlledProvider, new_conversation, submit
from tests.test_chat_integration import chat as chat

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("EA_VERIFY_REAL_REPORT") != "1",
        reason="需显式启用真实模型验收",
    ),
]


async def _complete(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    """仅在验收对话内批准模拟业务数据的导出。"""
    async with asyncio.timeout(240):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            if body["run"]["status"] not in {"pending", "running"}:
                return body
            approval = body.get("approval")
            if approval is not None:
                assert approval["tool_name"] == "create_deliverable"
                result = await client.post(
                    f"/api/runs/{run_id}/approvals/{approval['id']}/decide", json={"decision": "approve"}
                )
                result.raise_for_status()
            await asyncio.sleep(0.5)


async def test_real_model_creates_report_from_full_workbook(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前 250 行无二车间，借此核实实际分析读取原件。"""
    app, client, _ = chat
    runtime = app.state.chat_runtime
    assert runtime.settings.report_sandbox_image or runtime.settings.report_sandbox_broker_url
    runtime.provider = DeepSeekProvider(runtime.settings)
    scripts: list[dict[str, object]] = []

    async def record_sandbox(**arguments: Any) -> object:
        """执行真实容器，保留合成样本的脚本供验收失败时诊断。"""
        step = {"paths": list(arguments["files"]), "code": arguments["code"]}
        scripts.append(step)
        try:
            return await run_report_sandbox(**arguments)
        except ReportSandboxError as exc:
            step["error"] = exc.code
            raise

    monkeypatch.setattr("app.capabilities.builtins.reporting.run_report_sandbox", record_sandbox)

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "质检记录"
    sheet.append(["车间", "合格数"])
    for _ in range(300):
        sheet.append(["一车间", 2])
    for _ in range(50):
        sheet.append(["二车间", 5])
    content = BytesIO()
    workbook.save(content)
    workbook.close()

    conversation = await new_conversation(client)
    created = await client.post(
        f"/api/conversations/{conversation}/runs/attachments",
        data={
            "idempotency_key": "real-report",
            "content": (
                "请对上传的 Excel 原件做报表。先检视来源，再调用 Python 工具读取完整工作表，"
                "按车间求合格数总和，表格列名为车间、合格数，生成一张柱状图。最后用报表 ID 导出 Excel。"
            ),
        },
        files={
            "files": (
                "质检记录.xlsx",
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    created.raise_for_status()
    run_id = created.json()["run"]["id"]
    result = await _complete(client, run_id)
    audit = (await client.get(f"/api/runs/{run_id}/tool-calls")).json()
    steps = [(item["tool_name"], item["status"], item["error_code"]) for item in audit]
    assert result["run"]["status"] == "completed", steps
    reports = result["message"]["reports"]
    assert len(reports) == 1, steps
    info = (await client.get(f"/api/conversations/{conversation}/reports/{reports[0]['id']}")).json()
    expected = {"一车间": 600, "二车间": 250}
    assert info["columns"] == ["车间", "合格数"]
    assert dict(info["rows"]) == expected
    assert info["source_files"] == ["质检记录.xlsx"] and info["charts"][0]["type"] == "bar"
    attachments = result["message"]["attachments"]
    assert len(attachments) == 1, steps
    attachment = await client.get(f"/api/conversations/{conversation}/attachments/{attachments[0]['id']}/file")
    attachment.raise_for_status()
    saved = load_workbook(BytesIO(attachment.content), read_only=True)
    try:
        assert dict(list(saved.active.values)[1:]) == expected
    finally:
        saved.close()

    followup = await submit(client, conversation, "基于上次报表数据，只画二车间的柱状图。", "real-report-followup")
    scripts.clear()
    followup_result = await _complete(client, followup["run"]["id"])
    updated = followup_result["message"]["reports"]
    assert len(updated) == 1, (followup_result["message"]["content"], scripts)
    subset = (await client.get(f"/api/conversations/{conversation}/reports/{updated[0]['id']}")).json()
    assert subset["rows"] == [["二车间", 250]]
    history = (await client.get(f"/api/conversations/{conversation}/messages")).json()["items"]
    assert history[1]["reports"] == reports and history[3]["reports"] == updated
