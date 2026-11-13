"""本机命令的口令交互和安全错误输出验证。"""

import sys
from unittest.mock import AsyncMock, Mock

import pytest

from app.cli import accounts
from app.core.errors import ApplicationError


def test_initialization_uses_visible_password_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """密码通过明文交互输入，不再额外打印到日志。

    Args:
        monkeypatch: 替换终端输入与执行边界。
        capsys: 捕获公开输出。
    """
    execute = AsyncMock()
    monkeypatch.setattr(sys, "argv", ["accounts", "init", "--username", "admin", "--claim-legacy"])
    monkeypatch.setattr("builtins.input", Mock(side_effect=["local-test-password", "local-test-password"]))
    monkeypatch.setattr(accounts, "execute", execute)
    assert accounts.main() == 0
    assert execute.call_args.args[1] == "local-test-password"
    assert execute.call_args.args[0].claim_legacy
    assert "local-test-password" not in capsys.readouterr().out


def test_mismatched_password_does_not_modify_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """两次密码不一致时不进入数据库执行。

    Args:
        monkeypatch: 替换命令执行与输入。
    """
    execute = AsyncMock()
    monkeypatch.setattr(sys, "argv", ["accounts", "init"])
    monkeypatch.setattr("builtins.input", Mock(side_effect=["first-password", "second-password"]))
    monkeypatch.setattr(accounts, "execute", execute)
    assert accounts.main() == 2
    execute.assert_not_called()


def test_claim_command_needs_no_password_and_exposes_only_safe_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """显式认领由本机数据库权限控制，不要求在参数传密码。

    Args:
        monkeypatch: 替换命令执行。
        capsys: 捕获公开输出。
    """
    execute = AsyncMock(side_effect=ApplicationError("CLAIM_TARGET_INVALID", "目标不存在", 409))
    prompt = Mock()
    monkeypatch.setattr(sys, "argv", ["accounts", "claim-legacy"])
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(accounts, "execute", execute)
    assert accounts.main() == 1
    prompt.assert_not_called()
    assert "CLAIM_TARGET_INVALID" in capsys.readouterr().out
