"""在离线容器中执行授权报表分析，向调用方返回单个 JSON 值。"""

import asyncio
import base64
import binascii
import json
import logging
import math
import re
import uuid
from threading import BoundedSemaphore

MAX_REPORT_CODE_BYTES = 64 * 1024
MAX_REPORT_INPUT_BYTES = 16 * 1024 * 1024
MAX_REPORT_FILES = 16
MAX_REPORT_TIMEOUT_SECONDS = 120
MAX_REPORT_REQUEST_BYTES = 24 * 1024 * 1024
_MAX_OUTPUT_BYTES = 512 * 1024
_MAX_ERROR_BYTES = 32 * 1024
_MAX_BROKER_RESPONSE_BYTES = _MAX_OUTPUT_BYTES * 2
_DOCKER_CONTROL_TIMEOUT = 8.0
_REMOVE_RETRY_DELAY = 0.15
_SCRIPT_TIMEOUT_EXIT = 124
_BOOTSTRAP_FAILURE_EXIT = 125
_INPUT_NAME = re.compile(r"input_[1-9][0-9]{0,2}\.(?:csv|xlsx|json)")
_ERROR_MARKER = re.compile(rb"EA_REPORT_ERROR ([A-Za-z]{1,32}) ([0-9]{1,5})")
_START_MARKER = b"EA_REPORT_STARTED"
_WORKERS = BoundedSemaphore(2)
_LOGGER = logging.getLogger(__name__)
_SAFE_SCRIPT_ERRORS = frozenset(
    {
        "AttributeError",
        "FileNotFoundError",
        "ImportError",
        "IndentationError",
        "IndexError",
        "KeyError",
        "MemoryError",
        "ModuleNotFoundError",
        "NameError",
        "OverflowError",
        "PermissionError",
        "RuntimeError",
        "SyntaxError",
        "TypeError",
        "UnicodeDecodeError",
        "ValueError",
        "ZeroDivisionError",
    }
)
_ERROR_MESSAGES = {
    "unavailable": "报表沙箱暂不可用。",
    "invalid_request": "报表沙箱输入格式有误或超过大小限制。",
    "timeout": "报表脚本运行超时。",
    "output_limit": "报表脚本输出超过大小限制。",
    "script_error": "报表脚本运行失败。",
    "invalid_output": "报表脚本必须在标准输出中打印单个有效 JSON 值。",
    "busy": "报表沙箱正在处理其他任务，请稍后重试。",
}


class ReportSandboxError(Exception):
    """用于业务层、代理和模型修复提示的安全错误。"""

    def __init__(self, code: str, *, kind: str = "", line: int = 0) -> None:
        """构造错误，只接受受控的异常类型名和脚本行号。"""
        self.code = code if code in _ERROR_MESSAGES else "unavailable"
        self.kind = kind if self.code == "script_error" and kind in _SAFE_SCRIPT_ERRORS else ""
        self.line = line if self.code == "script_error" and 1 <= line <= 99999 else 0
        message = _ERROR_MESSAGES[self.code]
        if self.kind:
            location = f"第 {self.line} 行" if self.line else ""
            message = f"{message}{location}异常类型为 {self.kind}。"
        super().__init__(message)


def _reject_constant(value: str) -> object:
    """拒绝不属于 JSON 标准的数值常量。"""
    raise ValueError(value)


def _parse_float(value: str) -> float:
    """避免 JSON 中过大的指数在解析后变成无限值。"""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _parse_json(payload: bytes) -> object:
    """解析 UTF-8 JSON，并拒绝下游无法再次序列化的数值。"""
    return json.loads(payload.decode("utf-8"), parse_constant=_reject_constant, parse_float=_parse_float)


def _valid_timeout(timeout_seconds: object) -> bool:
    """检查超时上限，避免任务永久占用执行资源。"""
    return (
        isinstance(timeout_seconds, (int, float))
        and not isinstance(timeout_seconds, bool)
        and 0 < timeout_seconds <= MAX_REPORT_TIMEOUT_SECONDS
    )


def _validate_request(code: str, files: dict[str, bytes], timeout_seconds: float) -> None:
    """检查任务输入体积和服务端分配的固定文件名。"""
    try:
        valid_code = bool(code.strip()) and len(code.encode("utf-8")) <= MAX_REPORT_CODE_BYTES
    except UnicodeError as exc:
        raise ReportSandboxError("invalid_request") from exc
    if (
        not valid_code
        or not _valid_timeout(timeout_seconds)
        or len(files) > MAX_REPORT_FILES
        or any(not _INPUT_NAME.fullmatch(name) for name in files)
        or sum(map(len, files.values())) > MAX_REPORT_INPUT_BYTES
    ):
        raise ReportSandboxError("invalid_request")


def _encode_request(code: str, files: dict[str, bytes], timeout_seconds: float) -> bytes:
    """序列化报表任务，数据只通过标准输入或受控内部 HTTP 传递。"""
    return json.dumps(
        {
            "code": code,
            "files": {name: base64.b64encode(data).decode("ascii") for name, data in files.items()},
            "timeout_seconds": timeout_seconds,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_report_request(payload: bytes) -> tuple[str, dict[str, bytes], float]:
    """校验代理接收的任务数据，只允许报表代码、文件和超时。"""
    if len(payload) > MAX_REPORT_REQUEST_BYTES:
        raise ReportSandboxError("invalid_request")
    try:
        request = _parse_json(payload)
        if not isinstance(request, dict) or request.keys() != {"code", "files", "timeout_seconds"}:
            raise ValueError("unknown fields")
        code, encoded_files, timeout_seconds = request["code"], request["files"], request["timeout_seconds"]
        if not isinstance(code, str) or not isinstance(encoded_files, dict) or not _valid_timeout(timeout_seconds):
            raise ValueError("invalid fields")
        if len(encoded_files) > MAX_REPORT_FILES or any(not isinstance(value, str) for value in encoded_files.values()):
            raise ValueError("invalid files")
        files = {name: base64.b64decode(value, validate=True) for name, value in encoded_files.items()}
        timeout = float(timeout_seconds)
        _validate_request(code, files, timeout)
    except (ValueError, binascii.Error, RecursionError) as exc:
        raise ReportSandboxError("invalid_request") from exc
    return code, files, timeout


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    """限制读取总量，避免脚本将进程内存耗在持续输出上。"""
    output = bytearray()
    while chunk := await reader.read(min(64 * 1024, limit - len(output) + 1)):
        output.extend(chunk)
        if len(output) > limit:
            raise ReportSandboxError("output_limit")
    return bytes(output)


async def _send_stdin(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """分块写入标准输入，与输出读取并行以避免管道互锁。"""
    try:
        for offset in range(0, len(payload), 64 * 1024):
            writer.write(payload[offset : offset + 64 * 1024])
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        # 提前退出的脚本仍由退出码和受控标记报告原因。
        pass
    finally:
        writer.close()


async def _docker(
    args: list[str], *, payload: bytes = b"", stdout_limit: int = _MAX_OUTPUT_BYTES
) -> tuple[int, bytes, bytes]:
    """运行受信的 Docker CLI 命令，并限制两个输出流的体积。"""
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if payload else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout = asyncio.create_task(_read_bounded(process.stdout, stdout_limit))
    stderr = asyncio.create_task(_read_bounded(process.stderr, _MAX_ERROR_BYTES))
    writer = asyncio.create_task(_send_stdin(process.stdin, payload)) if process.stdin is not None else None
    try:
        result, errors, _ = await asyncio.gather(stdout, stderr, process.wait())
        if writer is not None:
            await writer
        return process.returncode or 0, result, errors
    finally:
        for task in (stdout, stderr, writer):
            if task is not None and not task.done():
                task.cancel()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # 管道中可能仍有缓冲区，先排空它们才能可靠回收 CLI 进程。
            stdout.cancel()
            stderr.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait(), return_exceptions=True)
        if writer is not None:
            await asyncio.gather(writer, return_exceptions=True)


def _create_args(name: str, image: str, runtime: str) -> list[str]:
    """构建容器隔离参数，SETUID 和 SETGID 只供受信引导程序降权。"""
    args = [
        "create",
        "--name",
        name,
        "--label",
        "enterprise-agent.report-sandbox=true",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--network",
        "none",
        "--ipc",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        "48",
        "--cpus",
        "1.0",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--log-driver",
        "none",
        "--tmpfs",
        "/inputs:rw,noexec,nosuid,nodev,size=18m,mode=0755",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--workdir",
        "/tmp",
    ]
    if runtime:
        args.extend(("--runtime", runtime))
    return [*args, "--", image]


async def _remove_container(name: str) -> None:
    """等待容器删除，兼容守护进程正在自动清理的情况。"""
    try:
        async with asyncio.timeout(_DOCKER_CONTROL_TIMEOUT):
            while True:
                try:
                    status, _, stderr = await _docker(["rm", "--force", name], stdout_limit=_MAX_ERROR_BYTES)
                except (OSError, ReportSandboxError):
                    break
                if status == 0 or b"No such container" in stderr:
                    return
                if b"removal of container" not in stderr:
                    break
                await asyncio.sleep(_REMOVE_RETRY_DELAY)
    except TimeoutError:
        pass
    _LOGGER.warning("无法确认报表沙箱容器已清理。")


def _script_failure(status: int, stderr: bytes) -> ReportSandboxError:
    """将退出码和安全诊断标记转换成可供模型修复的有限信息。"""
    lines = stderr.splitlines()
    if status == _BOOTSTRAP_FAILURE_EXIT or _START_MARKER not in lines:
        return ReportSandboxError("unavailable")
    if status == _SCRIPT_TIMEOUT_EXIT:
        return ReportSandboxError("timeout")
    for line in reversed(lines):
        if match := _ERROR_MARKER.fullmatch(line):
            return ReportSandboxError("script_error", kind=match[1].decode("ascii"), line=int(match[2]))
    return ReportSandboxError("script_error")


async def _run_local(image: str, runtime: str, payload: bytes, timeout_seconds: float) -> object:
    """在本地 Docker 守护进程运行报表并回收独立容器。"""
    if not image or not _WORKERS.acquire(blocking=False):
        raise ReportSandboxError("busy" if image else "unavailable")
    name = f"ea-report-{uuid.uuid4().hex}"
    try:
        try:
            async with asyncio.timeout(_DOCKER_CONTROL_TIMEOUT):
                status, _, _ = await _docker(_create_args(name, image, runtime), stdout_limit=_MAX_ERROR_BYTES)
            if status != 0:
                raise ReportSandboxError("unavailable")
            async with asyncio.timeout(timeout_seconds + _DOCKER_CONTROL_TIMEOUT):
                framed = f"{len(payload)}\n".encode("ascii") + payload
                status, stdout, stderr = await _docker(["start", "--attach", "--interactive", name], payload=framed)
        except OSError as exc:
            raise ReportSandboxError("unavailable") from exc
        except TimeoutError as exc:
            raise ReportSandboxError("timeout") from exc
        if status != 0:
            raise _script_failure(status, stderr)
        try:
            result = _parse_json(stdout)
            # 标准 JSON 允许转义代理码，UTF-8 API 响应不接受孤立代理码。
            json.dumps(result, ensure_ascii=False).encode("utf-8")
        except (ValueError, RecursionError) as exc:
            raise ReportSandboxError("invalid_output") from exc
        return result
    finally:
        try:
            cleanup = asyncio.create_task(_remove_container(name))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        finally:
            _WORKERS.release()


async def _run_remote(url: str, token: str, payload: bytes, timeout_seconds: float) -> object:
    """通过受信内部代理执行任务，并对代理响应设置上限。"""
    import httpx

    if not token:
        raise ReportSandboxError("unavailable")
    try:
        async with (
            asyncio.timeout(timeout_seconds + 3 * _DOCKER_CONTROL_TIMEOUT),
            httpx.AsyncClient(timeout=None, trust_env=False) as client,
            client.stream(
                "POST",
                f"{url.rstrip('/')}/v1/run",
                content=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            ) as response,
        ):
            output = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                output.extend(chunk)
                if len(output) > _MAX_BROKER_RESPONSE_BYTES:
                    raise ReportSandboxError("output_limit")
            envelope = _parse_json(bytes(output))
            if 200 <= response.status_code < 300 and isinstance(envelope, dict) and "result" in envelope:
                return envelope["result"]
            if response.status_code in {400, 422, 429, 503} and isinstance(envelope, dict):
                code, kind, line = envelope.get("error"), envelope.get("kind"), envelope.get("line")
                if isinstance(code, str):
                    raise ReportSandboxError(
                        code,
                        kind=kind if isinstance(kind, str) else "",
                        line=line if type(line) is int else 0,
                    )
    except TimeoutError as exc:
        raise ReportSandboxError("timeout") from exc
    except (httpx.HTTPError, ValueError, RecursionError) as exc:
        raise ReportSandboxError("unavailable") from exc
    raise ReportSandboxError("unavailable")


async def run_report_sandbox(
    *,
    image: str,
    runtime: str,
    code: str,
    files: dict[str, bytes],
    timeout_seconds: float,
    broker_url: str = "",
    broker_token: str = "",
) -> object:
    """执行隔离的 Python 报表脚本。

    Args:
        image: 本机受信的容器镜像名，使用代理时由代理配置决定。
        runtime: Docker 运行时名称，使用代理时由代理配置决定。
        code: 待执行的 Python 源码。
        files: 以服务端分配的固定文件名为键的授权输入文件。
        timeout_seconds: 脚本允许执行的秒数。
        broker_url: 受信代理的内部地址，为空时使用本机 Docker。
        broker_token: 用于认证代理请求的私有令牌。

    Returns:
        脚本在标准输出打印的 JSON 值。

    Raises:
        ReportSandboxError: 输入无效、沙箱不可用或执行失败。
    """
    _validate_request(code, files, timeout_seconds)
    payload = _encode_request(code, files, timeout_seconds)
    if broker_url:
        return await _run_remote(broker_url, broker_token, payload, timeout_seconds)
    return await _run_local(image, runtime, payload, timeout_seconds)
