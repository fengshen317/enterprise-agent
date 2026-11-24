"""用独立 PostgreSQL 验证报表引用、权限、追问和审批导出。"""

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.capabilities.builtins.reporting import report_specs
from app.schemas.chat import RunCreate
from app.services.chat_attachments import prepare_attachment
from app.services.tools import ToolExecutor
from tests.test_approvals_integration import wait_approval
from tests.test_chat_integration import ControlledProvider, new_conversation
from tests.test_chat_integration import chat as chat

pytestmark = pytest.mark.integration
ORIGINAL = "车间,合格数\n一车间,7\n二车间,9\n".encode()
REPORT = {
    "title": "按车间汇总",
    "explanation": "从原始 CSV 汇总合格数。",
    "columns": ["车间", "合格数"],
    "rows": [["一车间", 7], ["二车间", 9]],
    "charts": [{"title": "各车间合格数", "type": "bar", "x": "车间", "y": ["合格数"], "unit": "件"}],
}


async def _executor(app: FastAPI, run_id: UUID) -> ToolExecutor:
    """领取运行并按实际快照装配工具。"""
    runtime = app.state.chat_runtime
    return ToolExecutor(
        app.state.database.session_factory,
        runtime.settings,
        run_id,
        registry=runtime.registry,
        assembly=await runtime.assembler.assemble_for_run(run_id),
        approval_service=app.state.approval_service,
    )


async def test_report_snapshots_followups_and_export(
    chat: tuple[FastAPI, httpx.AsyncClient, ControlledProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    """图表数据可恢复、导出同源，且其它对话不能引用。"""
    app, client, _ = chat
    runtime = app.state.chat_runtime
    for spec in report_specs(runtime.settings):
        if runtime.registry.resolve(spec.key) is None:
            runtime.registry.register(spec)
    taken: list[dict[str, bytes]] = []

    async def sandbox(*, files: dict[str, bytes], **kwargs: object) -> object:
        """捕获沙箱收到的原始内容，替身专注于数据库和 HTTP 契约。"""
        taken.append(files)
        if kwargs["code"] == "invalid":
            return {**REPORT, "rows": [["一车间", True]]}
        return REPORT

    monkeypatch.setattr("app.capabilities.builtins.reporting.run_report_sandbox", sandbox)
    conversation = UUID(await new_conversation(client))
    first = await runtime.create_run(
        conversation,
        RunCreate(content="画各车间合格数", idempotency_key="report-test"),
        app.state.test_scope,
        uploads=[prepare_attachment(uuid4(), "质检.csv", ORIGINAL)],
    )
    run_id = first.run.id
    await runtime.store.begin_run(run_id)
    executor = await _executor(app, run_id)
    inventory = json.loads(await executor.invoke("inspect_analysis_data", {}))["data"]
    source_id = (await runtime.store.messages(conversation, 0, 10)).items[0].attachments[0].id
    assert inventory["files"] == [{"id": str(source_id), "name": "质检.csv", "format": "csv"}]

    args = {"source_ids": [str(source_id)], "code": "invalid"}
    invalid = json.loads(await executor.invoke("run_python_analysis", args))
    assert invalid["error_code"] == "REPORT_INVALID_RESULT"
    assert (await runtime.store.snapshot(run_id)).message.reports == []
    args["code"] = "print('报表脚本')"
    calculated = json.loads(await executor.invoke("run_python_analysis", args))
    assert calculated["ok"], calculated
    assert taken == [{"input_1.csv": ORIGINAL}] * 2
    report_id = calculated["data"]["report_id"]
    url = f"/api/conversations/{conversation}/reports/{report_id}"
    stored = await client.get(url)
    assert stored.is_success and stored.headers["cache-control"] == "no-store"
    assert stored.json()["rows"] == REPORT["rows"]
    assert stored.json()["code"] == args["code"]
    assert (await runtime.store.snapshot(run_id)).message.reports[0].id == UUID(report_id)

    export = asyncio.create_task(
        executor.invoke("create_deliverable", {"title": "质检汇总", "format": "csv", "report_id": report_id})
    )
    pending = await wait_approval(client, run_id)
    assert pending["args_preview"]["preview"]["首行"] == [["一车间", 7]]
    approved = await client.post(f"/api/runs/{run_id}/approvals/{pending['id']}/decide", json={"decision": "approve"})
    assert approved.is_success
    exported = json.loads(await export)
    assert exported["ok"], exported
    download = await client.get(f"/api/conversations/{conversation}/attachments/{exported['data']['id']}/file")
    assert download.is_success
    assert download.content.decode("utf-8-sig").splitlines() == ["车间,合格数", "一车间,7", "二车间,9"]
    await runtime.store.finish(run_id, "报表已生成", "completed")
    history = (await client.get(f"/api/conversations/{conversation}/messages")).json()["items"]
    assert history[1]["reports"] == [{"id": report_id, "title": REPORT["title"]}]

    second = await runtime.create_run(
        conversation, RunCreate(content="只看一车间", idempotency_key="followup"), app.state.test_scope
    )
    context = await runtime.store.begin_run(second.run.id)
    assert context is not None and report_id in context.messages[1].content
    followup = await _executor(app, second.run.id)
    result = json.loads(await followup.invoke("run_python_analysis", {"source_ids": [report_id], "code": "分析"}))
    assert result["ok"], result
    assert json.loads(taken[-1]["input_1.json"])["rows"] == REPORT["rows"]
    await runtime.store.finish(second.run.id, "追问完毕", "completed")

    other = UUID(await new_conversation(client))
    assert (await client.get(f"/api/conversations/{other}/reports/{report_id}")).status_code == 404
    third = await runtime.create_run(
        other, RunCreate(content="引用其它对话", idempotency_key="other"), app.state.test_scope
    )
    await runtime.store.begin_run(third.run.id)
    other_executor = await _executor(app, third.run.id)
    denied = json.loads(await other_executor.invoke("run_python_analysis", {"source_ids": [report_id], "code": "分析"}))
    assert denied["error_code"] == "REPORT_NOT_FOUND"
    await runtime.store.finish(third.run.id, "读取失败", "completed")
