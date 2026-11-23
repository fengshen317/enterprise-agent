"""Cron 与时区校验，所有计算统一输出 UTC 时间。"""

from datetime import UTC, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

from app.core.errors import ApplicationError
from app.services.schedule_calendar import CHINA_TIMEZONE, ScheduleCalendar, calendar_allows

CRON_FIELD_COUNT = 5


def validate_expression(expression: str) -> str:
    """校验标准五段 Cron 表达式，避免接受模糊的扩展语法。

    Args:
        expression: 客户端提交的 Cron 表达式。

    Returns:
        去空白后的表达式。

    Raises:
        ApplicationError: 表达式非法或不是五段。
    """
    cleaned = expression.strip()
    if len(cleaned.split()) != CRON_FIELD_COUNT or not croniter.is_valid(cleaned):
        raise ApplicationError("INVALID_CRON", "Cron 表达式需为 5 段：分 时 日 月 周", 422)
    return cleaned


def timezone_info(name: str) -> ZoneInfo:
    """加载 IANA 时区；Windows 依赖项目声明的 tzdata 数据。

    Args:
        name: IANA 时区名称，例如 Asia/Shanghai。

    Returns:
        可用于换算的时区对象。

    Raises:
        ApplicationError: 名称无法识别。
    """
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApplicationError("INVALID_TIMEZONE", "时区必须是有效的 IANA 名称，例如 Asia/Shanghai", 422) from exc


def next_occurrence(expression: str, timezone: str, base: datetime | None = None) -> datetime:
    """计算下一次触发时间，避免把本地时区误当 UTC。

    Args:
        expression: 已校验或待校验的五段 Cron。
        timezone: IANA 时区名称。
        base: 计算起点，默认当前 UTC 时间。

    Returns:
        带 UTC 时区的下一次触发时间。
    """
    upcoming, _ = next_scheduled_run(expression, timezone, "all_days", base)
    assert upcoming is not None
    return upcoming


def next_scheduled_run(
    expression: str, timezone: str, calendar_mode: ScheduleCalendar, base: datetime | None = None
) -> tuple[datetime | None, int | None]:
    """计算下一次合规触发，缺少日历时返回等待的公告年份。

    Args:
        expression: 五段 Cron。
        timezone: Cron 使用的 IANA 时区。
        calendar_mode: 按北京时间日期过滤候选触发的规则。
        base: 计算起点，默认当前 UTC 时间。

    Returns:
        下一次 UTC 触发时间或需要补充的日历年份。
    """
    cleaned = validate_expression(expression)
    zone = timezone_info(timezone)
    moment = base or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    while True:
        try:
            upcoming = cast(datetime, croniter(cleaned, moment.astimezone(zone)).get_next(datetime))
        except CroniterBadDateError as exc:
            raise ApplicationError("INVALID_CRON", "Cron 表达式没有可执行日期", 422) from exc
        if upcoming.tzinfo is None:
            upcoming = upcoming.replace(tzinfo=zone)
        day = upcoming.astimezone(CHINA_TIMEZONE).date()
        allowed = calendar_allows(calendar_mode, day)
        if allowed is None:
            return None, day.year
        if allowed:
            return upcoming.astimezone(UTC), None
        # 密集 Cron 遇到停市日时直接跳到下一天，避免逐分钟遍历。
        moment = datetime.combine(day + timedelta(days=1), time.min, CHINA_TIMEZONE) - timedelta(seconds=1)
