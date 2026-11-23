"""Cron 与时区换算的纯单元验证，不连接数据库或模型。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import ApplicationError
from app.schemas.schedule import CreateScheduledTaskArgs
from app.services.scheduling import next_occurrence, next_scheduled_run, timezone_info, validate_expression


def test_chat_schedule_accepts_agent_uuid_and_rejects_conversation_override() -> None:
    """工具支持 JSON 中的 Agent 标识，目标对话只能由服务端决定。"""
    arguments = {"name": "报告", "prompt": "生成报告", "cron_expression": "0 9 * * *"}
    agent_id = uuid4()
    assert CreateScheduledTaskArgs.model_validate(arguments).agent_profile_id is None
    args = CreateScheduledTaskArgs.model_validate({**arguments, "agent_profile_id": str(agent_id)})
    assert args.agent_profile_id == agent_id
    with pytest.raises(ValidationError):
        CreateScheduledTaskArgs.model_validate({**arguments, "agent_profile_id": "非 UUID"})
    with pytest.raises(ValidationError):
        CreateScheduledTaskArgs.model_validate({**arguments, "conversation_id": str(uuid4())})


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


def test_workday_and_market_calendars_treat_makeup_weekends_differently() -> None:
    """法定工作日包含补班，沪深交易日跳过同一个周日及假期。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    assert next_scheduled_run("0 9 * * *", "Asia/Shanghai", "cn_workday", base) == (
        datetime(2026, 1, 4, 1, tzinfo=UTC), None
    )
    assert next_scheduled_run("0 9 * * *", "Asia/Shanghai", "cn_a_share", base) == (
        datetime(2026, 1, 5, 1, tzinfo=UTC), None
    )


def test_market_calendar_uses_beijing_date_and_waits_for_new_announcement() -> None:
    """Cron 可用 UTC，日历按北京时间筛选，未覆盖的日期不会触发。"""
    assert next_scheduled_run("30 16 * * *", "UTC", "cn_a_share", datetime(2026, 9, 30, tzinfo=UTC)) == (
        datetime(2026, 10, 7, 16, 30, tzinfo=UTC), None
    )
    assert next_scheduled_run("0 9 * * *", "Asia/Shanghai", "cn_a_share", datetime(2026, 12, 31, 2, tzinfo=UTC)) == (
        None, 2027
    )
