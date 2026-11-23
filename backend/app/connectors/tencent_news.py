"""通过腾讯新闻官方 CLI 为当前用户查询新闻。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO

from app.connectors.base import ConnectorError, ConnectorToolDef

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50
_MAX_QUERY = 200
_MAX_CHANNEL = 40
_OUTPUT_BYTES = 128 * 1024
_TIMEOUT_SECONDS = 25
_LIMIT = {"type": "integer", "default": _DEFAULT_LIMIT, "minimum": 1, "maximum": _MAX_LIMIT}

TOOLS: tuple[ConnectorToolDef, ...] = (
    ConnectorToolDef(
        name="search",
        description="按关键词搜索腾讯新闻，保留原文链接、媒体来源等上游返回的信息。",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": _MAX_QUERY}, "limit": _LIMIT},
            "required": ["query"],
            "additionalProperties": False,
        },
        risk="read",
        approval="never",
    ),
    ConnectorToolDef(
        name="hot",
        description="查询腾讯新闻频道热点精选，适用于定时简报。频道例如要闻、科技、财经。",
        input_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "default": "要闻", "maxLength": _MAX_CHANNEL},
                "limit": _LIMIT,
            },
            "additionalProperties": False,
        },
        risk="read",
        approval="never",
    ),
)


def _executable() -> str:
    """查找官方安装目录或 PATH 中的原生二进制。

    Returns:
        原生可执行文件的绝对路径。
    """
    binary = "tencent-news-cli.exe" if os.name == "nt" else "tencent-news-cli"
    roots = [Path.home() / ".tencent-news-cli"]
    if local := os.environ.get("LOCALAPPDATA"):
        roots.insert(0, Path(local) / "enterprise-agent" / "tencent-news")
    if install := os.environ.get("TENCENT_NEWS_INSTALL"):
        roots.insert(0, Path(install))
    for root in roots:
        path = root / "bin" / binary
        if path.is_file():
            return str(path.resolve())
    if resolved := shutil.which(binary):
        return str(Path(resolved).resolve())
    raise ConnectorError("not_configured", "服务端需要安装腾讯新闻 CLI v1.0.23。")


def _command(name: str, arguments: dict[str, object]) -> list[str]:
    """把白名单参数转换为 CLI 参数。

    Args:
        name: 工具名称。
        arguments: 用户输入。

    Returns:
        不经过命令解释器的参数。
    """
    limit = arguments.get("limit", _DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        raise ConnectorError("invalid_arguments", f"新闻条数应为 1 到 {_MAX_LIMIT} 的整数。")
    if name == "search":
        field, value, maximum = "query", arguments.get("query"), _MAX_QUERY
    elif name == "hot":
        field, value, maximum = "channel", arguments.get("channel", "要闻"), _MAX_CHANNEL
    else:
        raise ConnectorError("unknown_tool", "未知的腾讯新闻工具。")
    if arguments.keys() - {field, "limit"} or not isinstance(value, str) or not value.strip():
        raise ConnectorError("invalid_arguments", "腾讯新闻查询参数不正确。")
    value = value.strip()
    if len(value) > maximum or not value.isprintable():
        raise ConnectorError("invalid_arguments", "查询参数过长或含有控制字符。")
    if name == "search":
        return ["search", "--limit", str(limit), "--", value]
    return ["hotspot", "premium", f"--channel={value}", "--limit", str(limit), "--format", "json"]


def _run(command: list[str], key: str) -> bytes:
    """在隔离的用户目录中执行查询，对两个输出流分别限制大小。

    Args:
        command: 已解析的程序与白名单参数。
        key: 当前用户绑定的 API Key。

    Returns:
        上游成功响应的标准输出。
    """
    with TemporaryDirectory(prefix="tencent-news-") as home:
        # 只传递网络和系统运行所需变量，避免子进程继承工作台的数据库或模型密钥。
        allowed = {"SYSTEMROOT", "WINDIR", "PATH", "LANG", "LC_ALL", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env.update(
            dict.fromkeys(("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "TMP", "TEMP"), home)
        )
        env["TENCENT_NEWS_APIKEY"] = key
        with (
            subprocess.Popen(
                command,
                cwd=home,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as proc,
            ThreadPoolExecutor(max_workers=2) as readers,
        ):

            def read(stream: IO[bytes]) -> bytes:
                """超限后立刻终止进程，避免无限输出占满内存。

                Args:
                    stream: 单个输出流。

                Returns:
                    有界输出。
                """
                data = stream.read(_OUTPUT_BYTES + 1)
                if len(data) > _OUTPUT_BYTES:
                    proc.kill()
                    raise ConnectorError("invalid_output", "腾讯新闻 CLI 输出超过限制，请减少查询条数。")
                return data

            assert proc.stdout is not None and proc.stderr is not None
            stdout = readers.submit(read, proc.stdout)
            stderr = readers.submit(read, proc.stderr)
            try:
                proc.wait(timeout=_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise ConnectorError("timeout", "腾讯新闻查询超时。") from None
            output, error = stdout.result(), stderr.result()
            if proc.returncode:
                diagnostic = (error + output).decode("utf-8", errors="replace").lower()
                if any(marker in diagnostic for marker in ("verifyapikey", "api key 无效", "unauthorized", "401")):
                    raise ConnectorError("auth_failed", "腾讯新闻 API Key 无效，请重新绑定。")
                if "unknown command" in diagnostic:
                    raise ConnectorError("command_failed", "腾讯新闻 CLI 命令不兼容，请安装 v1.0.23。")
                raise ConnectorError("command_failed", "腾讯新闻 CLI 查询失败，请检查服务和网络。")
            return output


async def invoke(name: str, arguments: dict[str, object], credentials: dict[str, str]) -> object:
    """使用当前绑定执行新闻查询，并保留上游原文与来源。

    Args:
        name: 搜索或热点工具名称。
        arguments: 查询参数。
        credentials: 当前用户的解密凭证。

    Returns:
        包含来源平台与上游完整结果的字典。

    Raises:
        ConnectorError: 凭证、参数、CLI 或上游响应异常。
    """
    key = credentials.get("api_key", "").strip()
    if not key:
        raise ConnectorError("missing_api_key", "请先绑定腾讯新闻 API Key。")
    args = _command(name, arguments)
    try:
        raw = await asyncio.to_thread(_run, [_executable(), *args], key)
        content = raw.decode("utf-8-sig").strip()
        if not content or key in content:
            raise ConnectorError("invalid_output", "腾讯新闻返回内容异常。")
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # 官方 search 没有 --format 参数，保留文本中的媒体名称和原文链接。
            if name != "search":
                raise ConnectorError("invalid_output", "腾讯新闻热点未返回有效 JSON。") from None
            result = content
    except UnicodeError:
        raise ConnectorError("invalid_output", "腾讯新闻返回了无效编码。") from None
    except (OSError, ValueError):
        raise ConnectorError("command_failed", "无法启动腾讯新闻 CLI。") from None
    return {"source": "腾讯新闻", "data": result}
