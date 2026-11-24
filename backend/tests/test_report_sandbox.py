"""验证报表沙箱的输入、输出、错误和容器回收约束。"""

import asyncio
import io
import os
import secrets
import subprocess
import time
import uuid
from collections.abc import Awaitable, Iterator

import httpx
import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from app.services import report_sandbox


def _run_locally(code: str, *, files: dict[str, bytes] | None = None, seconds: float = 5) -> Awaitable[object]:
    """启动 Docker 镜像或当前测试给定的 Docker 客户端替身。"""
    return report_sandbox.run_report_sandbox(
        image=os.getenv("EA_TEST_REPORT_SANDBOX_IMAGE", "enterprise-agent-report-sandbox:local"),
        runtime="",
        code=code,
        files=files or {},
        timeout_seconds=seconds,
    )


@pytest.mark.parametrize("name", ["../input_1.csv", "input_0.csv", "secret.xlsx", "input_1.csv\n"])
async def test_reject_invalid_input_name(name: str) -> None:
    """容器启动前拒绝不受控的路径。"""
    with pytest.raises(report_sandbox.ReportSandboxError) as error:
        await _run_locally("print('null')", files={name: b""})
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("seconds", [0, -1, 121, float("nan"), float("inf")])
async def test_reject_invalid_timeout(seconds: float) -> None:
    """容器启动前拒绝不可执行的超时参数。"""
    with pytest.raises(report_sandbox.ReportSandboxError) as error:
        await _run_locally("print('null')", seconds=seconds)
    assert error.value.code == "invalid_request"


def test_request_protocol_validates_payload_and_disallows_runtime() -> None:
    """验证代理传输保持输入字节，并拒绝请求指定执行环境。"""
    valid = b'{"code":"print(12)","files":{"input_1.csv":"YWJj"},"timeout_seconds":2}'
    code, files, seconds = report_sandbox.decode_report_request(valid)
    assert (code, files, seconds) == ("print(12)", {"input_1.csv": b"abc"}, 2)
    with pytest.raises(report_sandbox.ReportSandboxError) as error:
        report_sandbox.decode_report_request(valid[:-1] + b',"runtime":"other"}')
    assert error.value.code == "invalid_request"


async def test_output_reader_bounds_and_propagates_overflow() -> None:
    """允许输出恰好达到上限，并立即拒绝多出的字节。"""
    reader = asyncio.StreamReader()
    reader.feed_data(b"12345")
    reader.feed_eof()
    assert await report_sandbox._read_bounded(reader, 5) == b"12345"
    reader = asyncio.StreamReader()
    reader.feed_data(b"123456")
    with pytest.raises(report_sandbox.ReportSandboxError) as error:
        await report_sandbox._read_bounded(reader, 5)
    assert error.value.code == "output_limit"


@pytest.mark.parametrize("stdout", [b"{}\n{}", b"NaN", b"1e500", b'"\\ud800"'])
async def test_parse_rejects_invalid_report_stdout(monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> None:
    """拒绝多个 JSON 值或无法进入 UTF-8 JSON 响应的值。"""

    async def docker(args: list[str], **_kwargs: object) -> tuple[int, bytes, bytes]:
        """模拟执行成功但输出有误的脚本。"""
        return 0, stdout if args[0] == "start" else b"", b""

    monkeypatch.setattr(report_sandbox, "_docker", docker)
    with pytest.raises(report_sandbox.ReportSandboxError) as error:
        await _run_locally("print('null')")
    assert error.value.code == "invalid_output"


async def test_docker_cleanup_follows_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用任务被取消后等待专用容器回收。"""
    calls: list[list[str]] = []
    started = asyncio.Event()

    async def docker(args: list[str], **_kwargs: object) -> tuple[int, bytes, bytes]:
        """记录命令顺序，并让执行等待取消。"""
        calls.append(args)
        if args[0] == "start":
            started.set()
            await asyncio.Event().wait()
        return 0, b"", b""

    monkeypatch.setattr(report_sandbox, "_docker", docker)
    task = asyncio.ensure_future(_run_locally("print('null')"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call[0] for call in calls] == ["create", "start", "rm"]
    assert calls[0][2] == calls[1][-1] == calls[2][-1]


def _has_real_runner() -> pytest.MarkDecorator:
    """让普通单测保持不依赖 Docker，真实验收显式启用。"""
    return pytest.mark.skipif(not os.getenv("EA_TEST_REPORT_SANDBOX_IMAGE"), reason="需要预先构建沙箱镜像")


def _docker_reports() -> set[str]:
    """列出正在该 Docker 上存在的报表容器，仅用于验收回收。"""
    process = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=enterprise-agent.report-sandbox=true"],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    return set(process.stdout.splitlines())


@_has_real_runner()
async def test_real_runner_reads_csv_and_xlsx_with_isolation() -> None:
    """验证真实容器的数据聚合、低权限用户、只读输入与断网配置。"""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["count"])
    sheet.append([4])
    sheet.append([7])
    spreadsheet = io.BytesIO()
    workbook.save(spreadsheet)
    code = """import json, os
from pathlib import Path
import pandas as pd

data = Path('/inputs/input_1.csv')
workbook = Path('/inputs/input_2.xlsx')
values = [int(pd.read_csv(data)['count'].sum()), int(pd.read_excel(workbook)['count'].sum())]
try:
    data.write_text('cannot change')
except PermissionError:
    readonly = True
else:
    readonly = False
cap = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
print(json.dumps({'sums': values, 'uid': os.getuid(), 'readonly': readonly,
    'interfaces': sorted(p.name for p in Path('/sys/class/net').iterdir()),
    'capEff': int(cap['CapEff'].strip(), 16), 'capPrm': int(cap['CapPrm'].strip(), 16),
    'docker_socket': Path('/var/run/docker.sock').exists(),
    'environment': sorted(os.environ)}))
"""
    result = await _run_locally(code, files={"input_1.csv": b"count\n2\n3\n", "input_2.xlsx": spreadsheet.getvalue()})
    assert result == {
        "sums": [5, 11],
        "uid": 65532,
        "readonly": True,
        "interfaces": ["lo"],
        "capEff": 0,
        "capPrm": 0,
        "docker_socket": False,
        "environment": [
            "HOME",
            "LANG",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "PATH",
        ],
    }


@_has_real_runner()
@pytest.mark.parametrize(
    ("code", "seconds", "expected"),
    [
        ("import time\ntime.sleep(15)", 1, "timeout"),
        ("print('x' * (600 * 1024))", 5, "output_limit"),
        ("raise ValueError('企业业务内容')", 5, "script_error"),
    ],
)
async def test_real_runner_time_error_and_output_cleanup(code: str, seconds: float, expected: str) -> None:
    """真实容器在超时、异常和输出超限后均回收。"""
    existing = _docker_reports()
    with pytest.raises(report_sandbox.ReportSandboxError) as error:
        await _run_locally(code, seconds=seconds)
    assert error.value.code == expected
    if expected == "script_error":
        assert error.value.kind == "ValueError"
        assert error.value.line == 1
        assert "企业业务内容" not in str(error.value)
    assert not (_docker_reports() - existing)


@_has_real_runner()
async def test_real_runner_removes_cancelled_container() -> None:
    """在 Docker 确认容器启动后取消任务，再检查容器已经删除。"""
    existing = _docker_reports()
    execution = asyncio.ensure_future(_run_locally("import time\ntime.sleep(15)", seconds=15))
    try:
        started = time.monotonic()
        while not (_docker_reports() - existing):
            assert time.monotonic() - started < 8
            await asyncio.sleep(0.1)
    finally:
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert not (_docker_reports() - existing)


@pytest.fixture
def docker_broker() -> Iterator[tuple[str, str]]:
    """在本机环回地址启动独立验收代理，结束时只删除验收容器。"""
    image = os.getenv("EA_TEST_REPORT_BROKER_IMAGE")
    if not image:
        pytest.skip("需要预先构建代理镜像")
    name = f"ea-report-broker-test-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "EA_REPORT_SANDBOX_BROKER_TOKEN": token,
        "EA_REPORT_SANDBOX_IMAGE": os.environ.get(
            "EA_TEST_REPORT_SANDBOX_IMAGE",
            "enterprise-agent-report-sandbox:local",
        ),
    }
    subprocess.run(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "--rm",
            "--detach",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--publish",
            "127.0.0.1::8090",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly",
            "--env",
            "EA_REPORT_SANDBOX_IMAGE",
            "--env",
            "EA_REPORT_SANDBOX_BROKER_TOKEN",
            image,
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=15,
    )
    try:
        port = subprocess.run(
            ["docker", "port", name, "8090/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        yield f"http://{port.stdout.strip()}", token
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True, timeout=15)


async def test_real_broker_authentication_success_and_cancellation(docker_broker: tuple[str, str]) -> None:
    """经由真实代理读取输入，并在客户端断开后回收容器。"""
    url, token = docker_broker
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        started = time.monotonic()
        while True:
            try:
                assert (await client.get(f"{url}/health")).json() == {"status": "ok"}
                break
            except httpx.HTTPError:
                assert time.monotonic() - started < 8
                await asyncio.sleep(0.2)

    async def run(code: str, *, broker_token: str = token) -> object:
        """通过与 API 相同的客户端执行脚本。"""
        return await report_sandbox.run_report_sandbox(
            image="ignored",
            runtime="ignored",
            broker_url=url,
            broker_token=broker_token,
            code=code,
            files={"input_1.json": b'{"sum": 12}'},
            timeout_seconds=15,
        )

    with pytest.raises(report_sandbox.ReportSandboxError) as unauthorized:
        await run("print('null')", broker_token="incorrect-token")
    assert unauthorized.value.code == "unavailable"
    assert await run("from pathlib import Path\nprint(Path('/inputs/input_1.json').read_text())") == {"sum": 12}
    existing = _docker_reports()
    execution = asyncio.create_task(run("import time\ntime.sleep(15)"))
    try:
        started = time.monotonic()
        while not (_docker_reports() - existing):
            assert time.monotonic() - started < 8
            await asyncio.sleep(0.1)
    finally:
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    started = time.monotonic()
    while _docker_reports() - existing:
        assert time.monotonic() - started < 5
        await asyncio.sleep(0.1)
