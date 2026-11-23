"""验证腾讯新闻查询的命令契约、用户隔离与进程边界。"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.connectors import tencent_news as news
from app.connectors.base import ConnectorError


def test_commands() -> None:
    """固定官方 v1.0.23 的命令语法，并覆盖以连字符开头的搜索词。"""
    assert news._command("search", {"query": "--help"}) == ["search", "--limit", "10", "--", "--help"]
    assert news._command("hot", {}) == ["hotspot", "premium", "--channel=要闻", "--limit", "10", "--format", "json"]
    assert {t.name for t in news.TOOLS if t.risk == "read" and t.approval == "never"} == {"search", "hot"}


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search", {}),
        ("hot", {"channel": "财经\x00"}),
        ("hot", {"limit": True}),
        ("search", {"query": "新闻", "limit": 51}),
        ("hot", {"command": "update"}),
        ("feedback", {}),
    ],
)
def test_rejects_invalid_commands(name: str, args: dict[str, object]) -> None:
    """非法参数不得进入外部进程。

    Args:
        name: 测试工具名。
        args: 无效输入。
    """
    with pytest.raises(ConnectorError):
        news._command(name, args)


async def test_query_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证原文与来源可返回，凭证和畸形热点响应被拦截。

    Args:
        monkeypatch: 替换外部 CLI。
    """
    run = Mock(return_value="标题 https://news.qq.com/example".encode())
    monkeypatch.setattr(news, "_run", run)
    monkeypatch.setattr(news, "_executable", lambda: "news")
    credentials = {"api_key": "test-token"}
    assert await news.invoke("search", {"query": "科技"}, credentials) == {
        "source": "腾讯新闻",
        "data": "标题 https://news.qq.com/example",
    }
    with pytest.raises(ConnectorError, match="JSON"):
        await news.invoke("hot", {}, credentials)
    run.return_value = b'{"articles":[]}'
    assert await news.invoke("hot", {}, credentials) == {"source": "腾讯新闻", "data": {"articles": []}}
    run.return_value = b"unexpected test-token"
    with pytest.raises(ConnectorError, match="内容异常"):
        await news.invoke("search", {"query": "科技"}, credentials)
    with pytest.raises(ConnectorError, match="绑定"):
        await news.invoke("hot", {}, {})


async def test_process_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同用户并发调用仅能看到各自的密钥和临时目录。

    Args:
        monkeypatch: 模拟服务端的共享环境变量。
    """
    monkeypatch.setenv("TENCENT_NEWS_APIKEY", "host-key")
    monkeypatch.setenv("DATABASE_PASSWORD", "host-password")
    probe = (
        "import os; from pathlib import Path; "
        "assert not os.getenv('DATABASE_PASSWORD'); "
        "assert Path.cwd() == Path.home(); "
        "print(os.environ['TENCENT_NEWS_APIKEY']); print(Path.cwd())"
    )
    command = [sys.executable, "-c", probe]
    first, second = await asyncio.gather(*(asyncio.to_thread(news._run, command, k) for k in ["user-1", "user-2"]))
    first_key, first_dir = first.decode().splitlines()
    second_key, second_dir = second.decode().splitlines()
    assert (first_key, second_key) == ("user-1", "user-2")
    assert first_dir != second_dir
    assert await asyncio.gather(*(asyncio.to_thread(Path(p).exists) for p in (first_dir, second_dir))) == [False, False]
    assert os.environ["TENCENT_NEWS_APIKEY"] == "host-key"


def test_process_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时、超大输出、认证失败均应返回固定错误信息。

    Args:
        monkeypatch: 缩短测试用超时。
    """
    key = "example-user-key"
    script = "import sys; print('verifyAPIKey failed example-user-key', file=sys.stderr); sys.exit(1)"
    with pytest.raises(ConnectorError) as failure:
        news._run([sys.executable, "-c", script], key)
    assert failure.value.code == "auth_failed" and key not in failure.value.message
    for stream in ("stdout", "stderr"):
        with pytest.raises(ConnectorError, match="超过限制"):
            news._run([sys.executable, "-c", f"import sys; sys.{stream}.write('x' * {news._OUTPUT_BYTES + 1})"], key)
    monkeypatch.setattr(news, "_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(ConnectorError, match="超时"):
        news._run([sys.executable, "-c", "import time; time.sleep(30)"], key)
