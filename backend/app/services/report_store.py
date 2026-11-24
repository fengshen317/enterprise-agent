"""报表和源文件只在当前对话及其所有者的权限内使用。"""

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApplicationError
from app.models.conversation import ChatAttachment, ChatReport, Message, Run
from app.schemas.auth import ResourceScope
from app.schemas.chat import ChatAttachmentView
from app.schemas.report import ReportData, ReportRef, ReportView
from app.services.materials import personal_materials

TABULAR_FORMATS = {
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
MAX_SOURCE_BYTES = 16 * 1024**2
INVENTORY_LIMIT = 12


@dataclass(frozen=True)
class AnalysisInput:
    """授权原件或不可变的上一份报表数据。"""

    label: str
    extension: str
    data: bytes


async def report_refs(session: AsyncSession, message_ids: list[UUID]) -> dict[UUID, list[ReportRef]]:
    """为一页历史或一次快照批量读取引用。"""
    grouped: dict[UUID, list[ReportRef]] = defaultdict(list)
    if not message_ids:
        return grouped
    records = await session.execute(
        select(ChatReport.message_id, ChatReport.id, ChatReport.title)
        .where(ChatReport.message_id.in_(message_ids))
        .order_by(ChatReport.created_at, ChatReport.id)
    )
    for message_id, report_id, title in records:
        grouped[message_id].append(ReportRef(id=report_id, title=title))
    return grouped


async def read_report(
    session: AsyncSession, conversation_id: UUID, report_id: UUID, run: Run | None = None
) -> ChatReport:
    """读取调用方已鉴权的对话中的报表，执行时限制在当前输入之前。"""
    query = (
        select(ChatReport)
        .join(Message, Message.id == ChatReport.message_id)
        .where(
            ChatReport.id == report_id,
            Message.conversation_id == conversation_id,
        )
    )
    if run is not None:
        query = query.where(Message.sequence <= run.input_sequence + 1)
    report = await session.scalar(query)
    if report is None:
        raise ApplicationError("REPORT_NOT_FOUND", "未找到此对话中的报表", 404)
    return report


def report_view(row: ChatReport) -> ReportView:
    """只公开前端需要的报表数据和计算代码。"""
    return ReportView.model_validate({**row.data, "id": row.id, "source_files": row.source_files, "code": row.code})


async def source_inventory(session: AsyncSession, run: Run) -> dict[str, object]:
    """收集最近上传的表格和已有报表，源数据留在服务端。"""
    user_messages = await session.scalars(
        select(Message)
        .where(
            Message.conversation_id == run.conversation_id,
            Message.sequence <= run.input_sequence,
            Message.role == "user",
            Message.attachments != [],
        )
        .order_by(Message.sequence.desc())
        .limit(INVENTORY_LIMIT)
    )
    files: dict[UUID, dict[str, object]] = {}
    for message in user_messages:
        for attachment in message.attachments:
            view = ChatAttachmentView.model_validate(attachment)
            extension = TABULAR_FORMATS.get(view.media_type)
            if extension and len(files) < INVENTORY_LIMIT:
                files.setdefault(view.id, {"id": str(view.id), "name": view.filename, "format": extension})
    reports = await session.execute(
        select(ChatReport.id, ChatReport.title)
        .join(Message, Message.id == ChatReport.message_id)
        .where(
            Message.conversation_id == run.conversation_id,
            Message.sequence <= run.input_sequence + 1,
        )
        .order_by(Message.sequence.desc(), ChatReport.created_at.desc(), ChatReport.id)
        .limit(INVENTORY_LIMIT)
    )
    return {
        "files": list(files.values()),
        "reports": [{"id": str(identifier), "name": title, "format": "json"} for identifier, title in reports],
    }


async def load_inputs(
    session: AsyncSession, scope: ResourceScope, run: Run, identifiers: list[UUID]
) -> list[AnalysisInput]:
    """以请求顺序读取当轮可访问的数据，禁止按模型指定路径取文件。"""
    inputs: list[AnalysisInput] = []
    size = 0
    for identifier in identifiers:
        reference = (
            select(Message.id)
            .where(
                Message.conversation_id == run.conversation_id,
                Message.sequence <= run.input_sequence,
                Message.role == "user",
                Message.attachments.contains([{"id": str(identifier)}]),
            )
            .exists()
        )
        file = await session.scalar(personal_materials(scope).where(ChatAttachment.id == identifier, reference))
        if file is None:
            report = await read_report(session, run.conversation_id, identifier, run)
            data = ReportData.model_validate(report.data).model_dump_json().encode()
            source = AnalysisInput(report.title, "json", data)
        else:
            extension = TABULAR_FORMATS.get(file.media_type)
            if extension is None:
                raise ApplicationError("REPORT_UNSUPPORTED_FILE", "数据分析支持 CSV 和 Excel（.xlsx）", 422)
            source = AnalysisInput(file.filename, extension, file.data)
        size += len(source.data)
        if size > MAX_SOURCE_BYTES:
            raise ApplicationError("REPORT_INPUT_TOO_LARGE", "每次分析的输入合计最多 16 MiB", 422)
        inputs.append(source)
    return inputs
