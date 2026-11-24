"""授权表格检视与隔离编程分析，产物直接附到本次问答。"""

import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.agent import WorkspaceToolPolicy
from app.models.conversation import ChatReport, Message, Run
from app.models.tool_call import ToolCall
from app.schemas.chat import MAX_CHAT_ATTACHMENTS
from app.schemas.report import MAX_REPORT_CODE, ReportData
from app.services.report_sandbox import ReportSandboxError, run_report_sandbox
from app.services.report_store import AnalysisInput, load_inputs, source_inventory
from app.services.system_logs import emit_system_log

INSPECT_ANALYSIS_DATA = "inspect_analysis_data"
RUN_PYTHON_ANALYSIS = "run_python_analysis"
PREVIEW_ROWS = 3
REPORT_ATTEMPTS = 3
TOOL_CLEANUP_SECONDS = 8

# 使用固定检视脚本读取格式和少量样例，模型只在分析步骤提供代码。
PROFILE_CODE = """
import json
from pathlib import Path

import pandas as pd

def describe(frame):
    head = frame.iloc[:3, :30].map(lambda value: value[:150] if isinstance(value, str) else value)
    return {
        "rows": len(frame), "total_columns": len(frame.columns),
        "columns": [{"name": str(name)[:150], "type": str(kind)} for name, kind in frame.dtypes.iloc[:30].items()],
        "sample": json.loads(head.to_json(orient="values", date_format="iso")),
    }

profiles = []
for path in sorted(Path('/inputs').glob('input_*')):
    profile = {"path": str(path)}
    if path.suffix == '.xlsx':
        with pd.ExcelFile(path, engine='openpyxl') as book:
            profile['sheet_names'] = book.sheet_names[:20]
            profile['total_sheets'] = len(book.sheet_names)
            profile['sheets'] = [{"name": name, **describe(book.parse(name))} for name in book.sheet_names[:5]]
    elif path.suffix == '.csv':
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                frame = pd.read_csv(path, encoding=encoding, low_memory=False)
                break
            except UnicodeDecodeError:
                if encoding == 'gb18030':
                    raise
        profile.update({"encoding": encoding, **describe(frame)})
    else:
        report = json.loads(path.read_text(encoding='utf-8'))
        profile.update(describe(pd.DataFrame(report['rows'], columns=report['columns'])))
    profiles.append(profile)
print(json.dumps(profiles, ensure_ascii=False, allow_nan=False))
"""


class AnalysisSources(BaseModel):
    """只接受逻辑标识，文件路径与身份由服务端提供。"""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[UUID] = Field(default_factory=list, max_length=MAX_CHAT_ATTACHMENTS)

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values: list[UUID]) -> list[UUID]:
        """保证每个输入在沙箱中只出现一次。"""
        if len(set(values)) != len(values):
            raise ValueError("分析来源不能重复")
        return values


class PythonAnalysis(AnalysisSources):
    """模型给出 Python 源码与要分析的授权数据。"""

    source_ids: list[UUID] = Field(min_length=1, max_length=MAX_CHAT_ATTACHMENTS)
    code: str = Field(min_length=1, max_length=MAX_REPORT_CODE)


async def _authorized_inputs(context: ToolContext, identifiers: list[UUID]) -> list[AnalysisInput]:
    """每次执行前复核当前运行和材料归属，读取期间独占会话。"""
    async with context.factory() as session:
        scope, _ = await context.executor._scope_row(session)
        run = await session.get_one(Run, context.run_id)
        return await load_inputs(session, scope, run, identifiers)


async def _execute(context: ToolContext, code: str, sources: list[AnalysisInput]) -> object:
    """输入采用固定文件名，运行器只能访问本次传入的内容。"""
    settings = context.settings
    files = {f"input_{index}.{source.extension}": source.data for index, source in enumerate(sources, start=1)}
    try:
        return await run_report_sandbox(
            image=settings.report_sandbox_image,
            runtime=settings.report_sandbox_runtime,
            broker_url=settings.report_sandbox_broker_url,
            broker_token=settings.report_sandbox_broker_token.get_secret_value(),
            timeout_seconds=settings.report_sandbox_timeout_seconds,
            code=code,
            files=files,
        )
    except ReportSandboxError as exc:
        raise ApplicationError(f"REPORT_SANDBOX_{exc.code.upper()}", str(exc), 422) from exc


async def _inspect(context: ToolContext, arguments: BaseModel) -> object:
    """空参数给出来源目录，指定标识时返回真实字段和行数。"""
    assert isinstance(arguments, AnalysisSources)
    if arguments.source_ids:
        sources = await _authorized_inputs(context, arguments.source_ids)
        return {"profiles": await _execute(context, PROFILE_CODE, sources)}
    async with context.factory() as session:
        await context.executor._scope_row(session)
        run = await session.get_one(Run, context.run_id)
        return await source_inventory(session, run)


async def _analyze(context: ToolContext, arguments: BaseModel) -> object:
    """校验后原子保存报表与审计，模型只收到少量计算结果。"""
    assert isinstance(arguments, PythonAnalysis)
    identifier = context.tool_call_id
    if identifier is None:
        raise ApplicationError("REPORT_MISSING_TOOL_CALL", "缺少有效的数据分析调用", 409)
    sources = await _authorized_inputs(context, arguments.source_ids)
    raw = await _execute(context, arguments.code, sources)
    try:
        data = ReportData.model_validate(raw)
    except ValidationError as exc:
        hint = exc.errors(include_url=False, include_context=False, include_input=False)[0]["msg"]
        raise ApplicationError("REPORT_INVALID_RESULT", f"报表结构校验失败：{hint}", 422) from exc
    audit = {"report_id": str(identifier), "rows": len(data.rows), "charts": len(data.charts)}
    async with context.factory.begin() as session:
        await session.get_one(Run, context.run_id, with_for_update=True)
        scope, _ = await context.executor._scope_row(session)
        policy = await session.get(WorkspaceToolPolicy, (scope.workspace_id, RUN_PYTHON_ANALYSIS))
        if policy is not None and not policy.enabled:
            raise ApplicationError("TOOL_NOT_ALLOWED", "当前空间已停用数据分析", 403)
        message_id = (await session.execute(select(Message.id).where(Message.run_id == context.run_id))).scalar_one()
        call = await session.get_one(ToolCall, identifier, with_for_update=True)
        session.add(
            ChatReport(
                id=identifier,
                message_id=message_id,
                title=data.title,
                data=data.model_dump(mode="json"),
                source_files=[item.label for item in sources],
                code=arguments.code,
            )
        )
        # 同次提交让历史报表和成功审计共享终态，进程停止时不会出现半个结果。
        call.status, call.finished_at, call.result = "completed", datetime.now(UTC), json.dumps(audit)
    emit_system_log(
        component="tool",
        event="tool_finished",
        outcome="success",
        run_id=context.run_id,
        tool_call_id=identifier,
        tool_name=RUN_PYTHON_ANALYSIS,
    )
    return {
        **audit,
        "title": data.title,
        "explanation": data.explanation,
        "columns": data.columns,
        "sample": data.rows[:PREVIEW_ROWS],
    }


def report_specs(settings: Settings) -> tuple[ToolSpec, ToolSpec]:
    """将分析挂到主 Agent 的现有权限、超时和审计入口。"""
    timeout = settings.report_sandbox_timeout_seconds + TOOL_CLEANUP_SECONDS
    return (
        ToolSpec(
            key=INSPECT_ANALYSIS_DATA,
            version=1,
            display_name="检查报表数据",
            description="列出本对话上传的 CSV/Excel 和历史报表。传 source_ids 则读取工作表、字段、行数和少量样例。",
            args_model=AnalysisSources,
            risk="read",
            approval="never",
            child_allowed=False,
            timeout_seconds=timeout,
            quota_per_run=REPORT_ATTEMPTS,
            handler=_inspect,
            audit=lambda arguments, result: {"source_count": len(cast(AnalysisSources, arguments).source_ids)},
        ),
        ToolSpec(
            key=RUN_PYTHON_ANALYSIS,
            version=1,
            display_name="运行 Python 生成报表",
            description=(
                "用 Python/pandas/openpyxl 读取本对话 source_ids 指定的实际文件并生成图表。"
                "路径按 source_ids 顺序为 /inputs/input_1.csv（或 .xlsx、历史报表 .json）等；"
                "历史报表 JSON 请用 json.load(open('/inputs/input_1.json', encoding='utf-8')) 读取，"
                "再用 pd.DataFrame(data['rows'], columns=data['columns']) 建表。CSV 编码以检视结果为准。"
                "stdout 只输出一份标准 JSON，字段为 title、explanation（统计范围、公式和缺失值处理）、"
                "columns（唯一列名）、rows（最多249行）、charts（1到4张）。每张图字段为 title、"
                "type（bar或line）、x（维度列名）、y（数值列名数组）、可选 unit。"
                "用 json.loads(frame.to_json(orient='values', date_format='iso')) 将 DataFrame 转成 rows，"
                "它会把 NumPy 数值转换为 JSON 数值，并把缺失值转为 null。"
                "最后 print(json.dumps(result, ensure_ascii=False, allow_nan=False))。"
                "用 report_id 引用成功结果；只有用户明确要求导出时才另调用 create_deliverable。"
            ),
            args_model=PythonAnalysis,
            risk="read",
            approval="never",
            child_allowed=False,
            timeout_seconds=timeout,
            quota_per_run=REPORT_ATTEMPTS,
            handler=_analyze,
            audit=lambda arguments, result: {"report_id": cast(dict[str, object], result)["report_id"]},
        ),
    )
