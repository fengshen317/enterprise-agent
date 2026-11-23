"""验证对话附件、抽取文本，并限制发送给模型的资料体积。"""

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID, uuid5
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring  # type: ignore[import-untyped]
from fastapi import Request
from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils.exceptions import InvalidFileException  # type: ignore[import-untyped]
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfReader, apply_configuration
from pypdf.errors import PyPdfError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core.errors import ApplicationError
from app.schemas.chat import MAX_CHAT_ATTACHMENTS, ChatAttachmentView, RunCreate

MIB = 1024 * 1024
MAX_FILE_BYTES = 8 * MIB
MAX_MESSAGE_FILE_BYTES = 16 * MIB
MAX_MULTIPART_BYTES = MAX_MESSAGE_FILE_BYTES + 256 * 1024
MAX_TEXT = 12_000
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 25_000_000
MAX_PDF_PAGES = 30
MAX_OFFICE_ENTRIES = 512
MAX_OFFICE_UNCOMPRESSED = 24 * MIB
MAX_SHEETS = 10
MAX_ROWS = 250
MAX_COLUMNS = 30
CHAT_VISION_MODEL = "deepseek-flash"
ATTACHMENT_ONLY_QUESTION = "请帮我查看这些附件。"
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
IMAGE_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json"}
OFFICE_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True)
class PreparedAttachment:
    """文件经过边界校验后，随本轮消息原子保存。"""

    view: ChatAttachmentView
    data: bytes
    text: str


def _bounded_text(parts: Iterable[str], truncated: bool = False) -> tuple[str, bool]:
    """按段累计文字，保留精确的截断状态。"""
    chunks: list[str] = []
    remaining = MAX_TEXT
    for part in parts:
        if len(part) + 1 > remaining:
            chunks.append(part[:remaining])
            return "\n".join(chunks).strip(), True
        chunks.append(part)
        remaining -= len(part) + 1
    return "\n".join(chunks).strip(), truncated


def _pdf_text(data: bytes) -> tuple[str, bool]:
    """只读取 PDF 的文本层，限制页数和展开后的内容流。"""
    if not data.startswith(b"%PDF-"):
        raise ValueError("PDF 文件签名不匹配")
    with apply_configuration(
        maximum_declared_stream_length=8 * MIB,
        zlib_maximum_output_length=8 * MIB,
        brotli_maximum_output_length=8 * MIB,
        lzw_maximum_output_length=8 * MIB,
        run_length_maximum_output_length=8 * MIB,
        array_based_stream_maximum_output_length=8 * MIB,
        stream_decoding_work_maximum_length=16 * MIB,
        stream_parsing_work_maximum_count=200_000,
        page_tree_maximum_entries=5000,
        xform_maximum_invocations_per_extraction=100,
        jbig2dec_binary=None,
    ):
        reader = PdfReader(BytesIO(data), strict=True, xref_stream_maximum_entries=100_000)
        if reader.is_encrypted:
            raise ApplicationError("CHAT_ATTACHMENT_ENCRYPTED", "请解锁 PDF 后重新上传", 422)
        pages = reader.pages
        return _bounded_text((page.extract_text() for page in pages[:MAX_PDF_PAGES]), len(pages) > MAX_PDF_PAGES)


def _office_text(data: bytes, extension: str) -> tuple[str, bool]:
    """限制 Office ZIP 展开大小，仅提取正文或工作表的值。"""
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_OFFICE_ENTRIES or sum(item.file_size for item in entries) > MAX_OFFICE_UNCOMPRESSED:
            raise ApplicationError("CHAT_ATTACHMENT_TOO_COMPLEX", "Office 文件展开后过大，请拆分后上传", 422)
        archive.getinfo("[Content_Types].xml")
        if extension == ".docx":
            document = fromstring(archive.read("word/document.xml"))
            if document.tag != WORD_NS + "document":
                raise ValueError("Word 主文档格式错误")
            return _bounded_text(
                "".join(node.text or "" for node in paragraph.iter(WORD_NS + "t"))
                for paragraph in document.iter(WORD_NS + "p")
            )
        archive.getinfo("xl/workbook.xml")

    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)

    def cells() -> Iterable[str]:
        """只向模型提供缓存的单元格值，不执行工作簿公式。"""
        for sheet in workbook.worksheets[:MAX_SHEETS]:
            yield f"工作表：{sheet.title}"
            for row in sheet.iter_rows(min_row=1, max_row=MAX_ROWS, min_col=1, max_col=MAX_COLUMNS, values_only=True):
                if any(value is not None for value in row):
                    yield "\t".join("" if value is None else str(value) for value in row).rstrip("\t")

    try:
        restricted = len(workbook.worksheets) > MAX_SHEETS or any(
            (sheet.max_row or 0) > MAX_ROWS or (sheet.max_column or 0) > MAX_COLUMNS
            for sheet in workbook.worksheets[:MAX_SHEETS]
        )
        return _bounded_text(cells(), restricted)
    finally:
        workbook.close()


def attachment_identifier(namespace: UUID, filename: str, data: bytes) -> UUID:
    """根据归档范围及原件内容生成可重复提交的文件标识。"""
    return uuid5(namespace, f"{hashlib.sha256(data).hexdigest()}:{filename}")


def prepare_attachment(namespace: UUID, filename: str, data: bytes) -> PreparedAttachment:
    """验证扩展名与实际内容，并在当前归档范围内标识原件。"""
    filename = re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", re.split(r"[/\\]", filename)[-1]).strip()
    extension = PurePosixPath(filename).suffix.lower()
    if not data or not filename or len(filename) > 180:
        raise ApplicationError("CHAT_ATTACHMENT_INVALID", "附件不能为空，文件名最多 180 个字符", 422)
    if len(data) > MAX_FILE_BYTES:
        raise ApplicationError("CHAT_ATTACHMENT_TOO_LARGE", "单个附件最多 8 MB", 413)

    try:
        text, truncated = "", False
        if extension in IMAGE_FORMATS:
            image_format = IMAGE_FORMATS[extension]
            with Image.open(BytesIO(data), formats=[image_format]) as image:
                width, height = image.size
                if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                    raise ApplicationError("CHAT_ATTACHMENT_TOO_COMPLEX", "图片尺寸过大，请缩小后上传", 422)
                image.verify()
            media_type = IMAGE_MIME[image_format]
        elif extension in TEXT_TYPES:
            media_type = TEXT_TYPES[extension]
            try:
                decoded = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                decoded = data.decode("gb18030")
            if "\x00" in decoded:
                raise ValueError("文本包含二进制内容")
            text, truncated = _bounded_text([decoded])
        elif extension == ".pdf":
            media_type = "application/pdf"
            text, truncated = _pdf_text(data)
        elif extension in OFFICE_TYPES:
            media_type = OFFICE_TYPES[extension]
            text, truncated = _office_text(data, extension)
        else:
            raise ApplicationError("CHAT_ATTACHMENT_TYPE", "请选择受支持的图片或文档格式", 422)
    except (
        OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, SyntaxError,
        Image.DecompressionBombError, PyPdfError, BadZipFile, ParseError, DefusedXmlException, InvalidFileException,
    ) as exc:
        raise ApplicationError("CHAT_ATTACHMENT_INVALID", "附件内容无法读取，或与扩展名不符", 422) from exc

    text = text.replace("\x00", "").strip()
    view = ChatAttachmentView(
        id=attachment_identifier(namespace, filename, data), filename=filename, media_type=media_type,
        size_bytes=len(data), text_available=bool(text), text_truncated=truncated,
    )
    return PreparedAttachment(view, data, text)


@asynccontextmanager
async def attachment_form(request: Request) -> AsyncGenerator[FormData]:
    """流式限制上传体积，确保使用后关闭临时文件。"""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "multipart/form-data":
        raise ApplicationError("CHAT_ATTACHMENT_FORM_REQUIRED", "请使用附件上传表单", 415)

    async def bounded_stream() -> AsyncGenerator[bytes]:
        """在解析前检查图片和文档的总请求体积。"""
        length = 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > MAX_MULTIPART_BYTES:
                raise ApplicationError("CHAT_ATTACHMENTS_TOO_LARGE", "每条消息的附件合计最多 16 MB", 413)
            yield chunk

    try:
        form = await MultiPartParser(
            request.headers, bounded_stream(), max_files=MAX_CHAT_ATTACHMENTS, max_fields=3, max_part_size=100_000,
        ).parse()
    except MultiPartException as exc:
        raise ApplicationError("CHAT_ATTACHMENT_FORM_INVALID", "附件表单无效，每条消息最多 4 个附件", 422) from exc
    try:
        yield form
    finally:
        await form.close()


async def read_attachments(
    form: FormData, namespace: UUID, *, fields: tuple[str, ...] = (),
) -> list[PreparedAttachment]:
    """校验允许的表单字段，并按所选顺序处理上传。"""
    files = form.getlist("files")
    if set(form) - {"files", *fields} or not files:
        raise ApplicationError("CHAT_ATTACHMENT_FORM_INVALID", "附件表单缺少有效文件或包含未知字段", 422)
    attachments: list[PreparedAttachment] = []
    size = 0
    for file in files:
        if not isinstance(file, UploadFile):
            raise ApplicationError("CHAT_ATTACHMENT_FORM_INVALID", "请选择有效附件", 422)
        data = await file.read(MAX_FILE_BYTES + 1)
        size += len(data)
        if size > MAX_MESSAGE_FILE_BYTES:
            raise ApplicationError("CHAT_ATTACHMENTS_TOO_LARGE", "本次附件合计最多 16 MB", 413)
        attachments.append(await asyncio.to_thread(prepare_attachment, namespace, file.filename or "", data))
    ids = [item.view.id for item in attachments]
    if len(ids) != len(set(ids)):
        raise ApplicationError("CHAT_ATTACHMENT_DUPLICATE", "请移除重复的附件", 422)
    return attachments


async def parse_attachment_run(request: Request, namespace: UUID) -> tuple[RunCreate, list[PreparedAttachment]]:
    """在现有运行参数上补充经验证的上传文件。"""
    async with attachment_form(request) as form:
        content = form.get("content", "")
        refs = form.get("attachment_ids", "[]")
        if not isinstance(content, str) or not isinstance(refs, str):
            raise ApplicationError("CHAT_ATTACHMENT_FORM_INVALID", "问题格式不正确", 422)
        try:
            payload = RunCreate.model_validate({
                "content": content if content.strip() else ATTACHMENT_ONLY_QUESTION,
                "idempotency_key": form.get("idempotency_key"),
                "attachment_ids": json.loads(refs),
            })
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ApplicationError("CHAT_ATTACHMENT_FORM_INVALID", "问题内容或提交标识格式不正确", 422) from exc
        return payload, await read_attachments(form, namespace, fields=("content", "idempotency_key", "attachment_ids"))


def attachment_context(view: ChatAttachmentView, text: str) -> str:
    """以结构化普通资料向模型明确文件状态及来源。"""
    info: dict[str, str | bool] = {"文件名": view.filename}
    if view.media_type in IMAGE_MIME.values():
        info["说明"] = "图片仅在发送或重新附加的这一轮提供视觉输入。"
    elif text:
        info.update({"提取文字": text, "部分内容未读取": view.text_truncated})
    else:
        info["说明"] = "未提取到文本，不能根据文件名推测内容。"
    return json.dumps(info, ensure_ascii=False)
