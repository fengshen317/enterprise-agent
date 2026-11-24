"""只在私有网络暴露的沙箱代理，负责在容器部署中访问 Docker。"""

import asyncio
import hmac
import json
import os
import select
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from socketserver import BaseServer
from threading import BoundedSemaphore

from app.services.report_sandbox import (
    MAX_REPORT_REQUEST_BYTES,
    ReportSandboxError,
    decode_report_request,
    run_report_sandbox,
)

_MAX_CONNECTIONS = 8
_CONNECTION_TIMEOUT = 8.0
_STATUS_BY_ERROR = {"invalid_request": 400, "unavailable": 503, "busy": 429}


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """只从代理部署环境读取的可信配置。"""

    image: str
    runtime: str
    token: str


class BrokerServer(ThreadingHTTPServer):
    """限制并行连接，避免内部客户端错误耗尽代理线程。"""

    def __init__(self, config: BrokerConfig) -> None:
        """在专用容器网络监听报表执行请求。"""
        self.config = config
        self.slots = BoundedSemaphore(_MAX_CONNECTIONS)
        super().__init__(("0.0.0.0", 8090), BrokerHandler)

    def process_request(self, request: socket | tuple[bytes, socket], client_address: tuple[str, int]) -> None:
        """连接数达到上限时直接释放 Socket。"""
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            if isinstance(request, socket):
                request.settimeout(_CONNECTION_TIMEOUT)
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket | tuple[bytes, socket], client_address: tuple[str, int]) -> None:
        """处理连接并回收并发配额。"""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: socket | tuple[bytes, socket], client_address: tuple[str, int]) -> None:
        """代理日志只记录静态故障提示，避免错误正文泄漏。"""
        print("报表沙箱代理请求处理失败。", file=sys.stderr)


class BrokerHandler(BaseHTTPRequestHandler):
    """对任务数据执行鉴权、体积校验和错误映射。"""

    def __init__(self, request: socket, client_address: tuple[str, int], server: BaseServer) -> None:
        """读取当前代理的可信配置。"""
        assert isinstance(server, BrokerServer)
        self.config = server.config
        super().__init__(request, client_address, server)

    def log_message(self, format: str, *args: object) -> None:
        """避免 HTTP 错误日志记录请求携带的内容。"""

    def _json(self, status: int, body: object) -> None:
        """发送长度明确的 UTF-8 JSON 响应。"""
        result = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def do_GET(self) -> None:
        """提供不含任务信息的健康检查。"""
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "unavailable"})

    def _read_payload(self) -> bytes:
        """拒绝缺少固定长度、过大或提前断开的请求体。"""
        content_length = self.headers.get_all("Content-Length")
        if (
            not content_length
            or len(content_length) != 1
            or self.headers.get("Transfer-Encoding")
            or not content_length[0].isascii()
            or not content_length[0].isdecimal()
            or len(content_length[0]) > 8
        ):
            raise ReportSandboxError("invalid_request")
        size = int(content_length[0])
        if not 0 < size <= MAX_REPORT_REQUEST_BYTES:
            raise ReportSandboxError("invalid_request")
        payload = self.rfile.read(size)
        if len(payload) != size:
            raise ReportSandboxError("invalid_request")
        return payload

    async def _watch_connection(self) -> None:
        """检测 API 连接关闭，以便远程调用取消时回收容器。"""
        while True:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                # 代理只处理每条连接的一个请求，关闭或新增数据均终止当前任务。
                return
            await asyncio.sleep(0.2)

    async def _execute(self, code: str, files: dict[str, bytes], timeout_seconds: float) -> object:
        """执行报表任务，并在 API 连接断开后主动取消任务。"""
        job = asyncio.create_task(
            run_report_sandbox(
                image=self.config.image,
                runtime=self.config.runtime,
                code=code,
                files=files,
                timeout_seconds=timeout_seconds,
            )
        )
        connection = asyncio.create_task(self._watch_connection())
        try:
            await asyncio.wait((job, connection), return_when=asyncio.FIRST_COMPLETED)
            if job.done():
                return job.result()
            raise ConnectionAbortedError
        finally:
            connection.cancel()
            job.cancel()
            await asyncio.gather(job, connection, return_exceptions=True)

    def do_POST(self) -> None:
        """执行代理唯一允许的动作，镜像和运行时由部署配置指定。"""
        if self.path != "/v1/run":
            self._json(404, {"error": "unavailable"})
            return
        authorization = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(authorization, f"Bearer {self.config.token}".encode()):
            self._json(403, {"error": "unavailable"})
            return
        try:
            code, files, timeout = decode_report_request(self._read_payload())
            result = asyncio.run(self._execute(code, files, timeout))
            self._json(200, {"result": result})
        except ReportSandboxError as exc:
            self._json(
                _STATUS_BY_ERROR.get(exc.code, 422),
                {"error": exc.code, "kind": exc.kind, "line": exc.line},
            )
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # API 已取消请求时无须发送响应。
            return


def main() -> int:
    """读取代理部署配置并启动 HTTP 服务。"""
    config = BrokerConfig(
        image=os.environ.get("EA_REPORT_SANDBOX_IMAGE", "enterprise-agent-report-sandbox:local"),
        runtime=os.environ.get("EA_REPORT_SANDBOX_RUNTIME", ""),
        token=os.environ.get("EA_REPORT_SANDBOX_BROKER_TOKEN", ""),
    )
    if not config.token or not config.image:
        print("报表沙箱代理需要部署方配置认证令牌和镜像。", file=sys.stderr)
        return 1
    with BrokerServer(config) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
