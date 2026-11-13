"""Cron 与时区换算的纯单元验证，不连接数据库或模型。"""

from datetime import UTC, datetime

import pytest

from app.core.errors import ApplicationError
from app.services.scheduling import next_occurrence, timezone_info, validate_expression


def test_next_occurrence_converts_local_clock_to_utc() -> None:
    """北京时间 9 点应换算为 01:00 UTC。"""
    result = next_occurrence("0 9 * * *", "Asia/Shanghai", datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert result == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)


def test_next_occurrence_supports_interval_and_weekdays() -> None:
    """间隔表达式与工作日范围都应得到确定的下一次时间。"""
    assert next_occurrence("*/15 * * * *", "UTC", datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 1, 1, 0, 15, tzinfo=UTC
    )
    # 2026-01-03 是周六，下一个工作日为周一 09:00（北京时间）。
    assert next_occurrence("0 9 * * 1-5", "Asia/Shanghai", datetime(2026, 1, 3, 0, 0, tzinfo=UTC)) == datetime(
        2026, 1, 5, 1, 0, tzinfo=UTC
    )


def test_validate_expression_trims_whitespace() -> None:
    """首尾空白不改变 Cron 语义。"""
    assert validate_expression(" 0 9 * * * ") == "0 9 * * *"


@pytest.mark.parametrize("expression", ["0 9 * *", "0 0 9 * * *", "not-a-cron"])
def test_invalid_cron_is_rejected(expression: str) -> None:
    """只接受标准五段表达式，避免秒级扩展产生歧义。"""
    with pytest.raises(ApplicationError) as caught:
        validate_expression(expression)
    assert caught.value.code == "INVALID_CRON"


def test_invalid_timezone_is_rejected() -> None:
    """非 IANA 名称必须在写入前失败。"""
    with pytest.raises(ApplicationError) as caught:
        timezone_info("Asia/Not-A-Real-Zone")
    assert caught.value.code == "INVALID_TIMEZONE"
