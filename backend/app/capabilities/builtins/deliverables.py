"""把已审批的问答交付物保存到用户当前空间的个人材料。"""

import asyncio
import csv
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO, StringIO
from typing import Annotated, Literal, Self
from uuid import UUID

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Font  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app.capabilities.spec import ToolContext, ToolSpec
from app.core.config import Settings
from app.core.errors import ApplicationError
from app.models.agent import WorkspaceToolPolicy
from app.models.approval import Approval, ApprovalEvent
from app.models.conversation import Message, Run
from app.models.tool_call import ToolCall
from app.schemas.report import ReportData
from app.services.chat_attachments import MAX_COLUMNS, MAX_ROWS, PreparedAttachment, prepare_attachment
from app.services.materials import MaterialStore
from app.services.report_store import read_report
from app.services.system_logs import emit_system_log

CREATE_DELIVERABLE = "create_deliverable"
MAX_CONTENT = 32_000
MAX_CELL = 2_000
MAX_NAME = 100
MAX_COLUMN_WIDTH = 48
EXCEL_INTEGER_DIGITS = 15
CellText = Annotated[str, Field(max_length=MAX_CELL)]
Cell = CellText | int | float | bool | None
Row = Annotated[list[Cell], Field(max_length=MAX_COLUMNS)]
UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class DeliverableArgs(BaseModel):
    """报告接收 Markdown 正文，表格接收列名和等宽数据行。"""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=MAX_NAME, description="文件标题，不含目录或扩展名")
    format: Literal["md", "csv", "xlsx"]
    markdown: str = Field(default="", max_length=MAX_CONTENT)
    columns: list[CellText] = Field(default_factory=list, max_length=MAX_COLUMNS)
    rows: list[Row] = Field(default_factory=list, max_length=MAX_ROWS - 1)
    report_id: Annotated[UUID, Field(strict=False)] | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        """拒绝路径、控制字符和系统保留名，保证保存名与审批预览一致。"""
        title = value.strip()
        if not title or UNSAFE_TEXT.search(title) or re.search(
            r'[<>:"/\\|?*\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', title
        ):
            raise ValueError("请使用可作为文件名的标题")
        if title.endswith(".") or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", title, re.I):
            raise ValueError("请更换文件标题")
        return title

    @model_validator(mode="after")
    def validate_contents(self) -> Self:
        """限制总量并拒绝错列数据，避免导出时静默丢弃内容。"""
        if self.report_id is not None:
            if self.format == "md" or self.markdown or self.columns or self.rows:
                raise ValueError("报表导出使用 CSV 或 Excel，直接提供 report_id")
            return self
        if self.format == "md":
            if not self.markdown.strip() or self.columns or self.rows or UNSAFE_TEXT.search(self.markdown):
                raise ValueError("Markdown 报告需要有效正文")
            return self
        if self.markdown or not self.columns or any(not col.strip() for col in self.columns):
            raise ValueError("表格需要非空列名")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("每一行都必须与表头列数一致")
        rows: list[Sequence[Cell]] = [self.columns, *self.rows]
        texts = [str(cell) for row in rows for cell in row if cell is not None]
        if sum(map(len, texts)) > MAX_CONTENT or any(UNSAFE_TEXT.search(text) for text in texts):
            raise ValueError("表格内容过大或含无效字符")
        return self

    @property
    def filename(self) -> str:
        """生成审批、下载和工具审计共用的文件名。"""
        suffix = "." + self.format
        return self.title if self.title.lower().endswith(suffix) else self.title + suffix


def render_deliverable(args: DeliverableArgs) -> bytes:
    """把结构化数据编码为可下载的原件；字符串不会被写为 Excel 公式。

    Args:
        args: 经过结构和容量校验的文件内容。

    Returns:
        对应格式的文件字节。
    """
    if args.format == "md":
        return args.markdown.encode("utf-8")
    rows: list[Sequence[Cell]] = [args.columns, *args.rows]
    if args.format == "csv":
        output = StringIO(newline="")
        writer = csv.writer(output)
        for row in rows:
            writer.writerow(
                "'" + cell if isinstance(cell, str) and cell.lstrip().startswith(("=", "+", "-", "@")) else cell
                for cell in row
            )
        return output.getvalue().encode("utf-8-sig")

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "数据"
    widths = [len(label) for label in args.columns]
    for row_index, values in enumerate(rows, start=1):
        for column, value in enumerate(values, start=1):
            # Excel 数值最多保留十五位有效数字，长整型编号按文本保存才能原样取回。
            if type(value) is int and len(str(abs(value))) > EXCEL_INTEGER_DIGITS:
                value = str(value)
            cell = sheet.cell(row=row_index, column=column, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            if row_index == 1:
                cell.font = Font(bold=True)
            widths[column - 1] = max(widths[column - 1], len(str(value or "")))
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = min(MAX_COLUMN_WIDTH, width * 2 + 4)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


async def _resolve_export(arguments: DeliverableArgs, context: ToolContext) -> DeliverableArgs:
    """按本轮对话的权限加载已保存的报表行，导出使用同一份数据。"""
    if arguments.report_id is None:
        return arguments
    async with context.factory() as session:
        _, conversation_id = await context.executor._scope_row(session)
        run = await session.get_one(Run, context.run_id)
        row = await read_report(session, conversation_id, arguments.report_id, run)
        data = ReportData.model_validate(row.data)
    return DeliverableArgs(title=arguments.title, format=arguments.format, columns=data.columns, rows=data.rows)


def deliverable_spec(settings: Settings) -> ToolSpec:
    """为主助手注册需要逐次确认的私有文件写入。"""

    async def preview(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        """在批准前展示实际文件名和内容样例。"""
        assert isinstance(arguments, DeliverableArgs)
        actual = await _resolve_export(arguments, context)
        return {
            "summary": f"保存文件「{actual.filename}」到我的材料",
            "effect": "仅当前用户在当前工作空间可访问",
            "preview": actual.markdown[:500]
            if actual.format == "md"
            else {"表头": actual.columns, "数据行数": len(actual.rows), "首行": actual.rows[:1]},
        }

    async def handler(context: ToolContext, arguments: BaseModel) -> object:
        """用本次调用标识保存文件，重试不重复创建材料。"""
        assert isinstance(arguments, DeliverableArgs)
        if context.tool_call_id is None:
            raise ApplicationError("APPROVAL_REQUIRED", "文件交付需要有效的工具调用", 409)
        actual = await _resolve_export(arguments, context)
        data = await asyncio.to_thread(render_deliverable, actual)
        file = await asyncio.to_thread(prepare_attachment, context.tool_call_id, arguments.filename, data)
        # Excel 包含生成时间；同一调用以业务标识去重，不能依赖二进制哈希。
        view = file.view.model_copy(update={"id": context.tool_call_id})
        attachment = PreparedAttachment(view, file.data, file.text)
        async with context.factory.begin() as session:
            await session.scalar(select(Run.id).where(Run.id == context.run_id).with_for_update())
            scope, conversation_id = await context.executor._scope_row(session)
            approval = await session.scalar(
                select(Approval)
                .where(
                    Approval.tool_call_id == view.id,
                    Approval.run_id == context.run_id,
                    Approval.tool_name == CREATE_DELIVERABLE,
                )
                .with_for_update()
            )
            payload = json.dumps(arguments.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            if (
                approval is None
                or approval.status not in {"approved", "executed"}
                or approval.workspace_id != scope.workspace_id
                or approval.decided_by_user_id != scope.user_id
                or approval.args_digest != hashlib.sha256(payload.encode()).hexdigest()
            ):
                raise ApplicationError("APPROVAL_REQUIRED", "无法核实文件生成审批", 409)
            policy = await session.get(WorkspaceToolPolicy, (scope.workspace_id, CREATE_DELIVERABLE))
            if policy is not None and not policy.enabled:
                raise ApplicationError("TOOL_NOT_ALLOWED", "当前空间已停用文件生成工具", 403)
            rows = await MaterialStore.persist(session, scope, [view.id], [attachment], conversation_id)
            saved = rows[0]
            metadata = {"id": str(saved.id), "name": saved.filename, "size_bytes": saved.size_bytes}
            first_execution = approval.status == "approved"
            if first_execution:
                # 原件与下载凭据一起提交，服务中断也不会让已保存的文件显示为创建失败。
                now = datetime.now(UTC)
                message = (await session.scalars(select(Message).where(
                    Message.run_id == context.run_id
                ).with_for_update())).one()
                message.attachments = [*message.attachments, view.model_dump(mode="json")]
                call = await session.get_one(ToolCall, view.id, with_for_update=True)
                call.status, call.finished_at, call.result = "completed", now, json.dumps(metadata, ensure_ascii=False)
                approval.status, approval.updated_at, approval.executed_at = "executed", now, now
                session.add(ApprovalEvent(approval_id=approval.id, action="executed", actor_role="system"))
        if first_execution:
            emit_system_log(
                component="tool",
                event="tool_finished",
                outcome="success",
                run_id=context.run_id,
                tool_call_id=view.id,
                tool_name=CREATE_DELIVERABLE,
            )
        return {**metadata, "url": f"/materials/{view.id}"}

    def audit(arguments: BaseModel, result: object) -> dict[str, object]:
        """审计仅包含文件元数据和站内引用。"""
        assert isinstance(result, dict)
        return {key: result[key] for key in ("id", "name", "size_bytes")}

    return ToolSpec(
        key=CREATE_DELIVERABLE,
        version=1,
        display_name="生成可下载文件",
        description=(
            "生成 Markdown 报告或 CSV、Excel 表格并保存到我的材料。"
            "导出已有数据分析结果时只传 report_id，不传 columns 或 rows。"
        ),
        args_model=DeliverableArgs,
        risk="write",
        approval="always",
        child_allowed=False,
        quota_per_run=3,
        timeout_seconds=settings.tool_timeout_seconds,
        handler=handler,
        audit=audit,
        approval_preview=preview,
    )
