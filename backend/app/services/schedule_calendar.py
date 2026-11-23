"""定时任务使用的中国工作日与沪深交易日历。"""

from datetime import date, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

ScheduleCalendar = Literal["all_days", "cn_workday", "cn_a_share"]
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _dates(*ranges: tuple[str, str]) -> frozenset[date]:
    """展开官方通知中的闭区间，包含首尾日期。"""
    days: set[date] = set()
    for start_text, end_text in ranges:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        days.update(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    return frozenset(days)


# 国办发明电〔2025〕7号、〔2026〕10号；2026 年沪深休市通知的假期区间相同。
_HOLIDAYS_2026 = _dates(
    ("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"), ("2026-10-01", "2026-10-07"),
)
_WORKDAY_HOLIDAYS = {
    2026: _HOLIDAYS_2026,
    2027: _dates(
        ("2027-01-01", "2027-01-03"), ("2027-02-05", "2027-02-13"),
        ("2027-04-03", "2027-04-05"), ("2027-05-01", "2027-05-05"),
        ("2027-06-09", "2027-06-09"), ("2027-09-15", "2027-09-15"), ("2027-10-01", "2027-10-07"),
    ),
}
_MAKEUP_WORKDAYS = frozenset(map(date.fromisoformat, (
    "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09", "2026-09-20", "2026-10-10",
    "2027-01-31", "2027-02-14", "2027-05-08", "2027-09-26", "2027-10-09",
)))
# 逐年采用沪深公告，未公告年份不能从国务院调休规则推断开市安排。
_MARKET_HOLIDAYS = {2026: _HOLIDAYS_2026}
CALENDAR_YEARS = {"cn_workday": tuple(_WORKDAY_HOLIDAYS), "cn_a_share": tuple(_MARKET_HOLIDAYS)}
CALENDAR_LABELS = {"all_days": "不筛选日期", "cn_workday": "中国法定工作日", "cn_a_share": "沪深 A 股交易日"}


def calendar_allows(mode: ScheduleCalendar, day: date) -> bool | None:
    """返回北京时间当天是否可触发；官方日历未覆盖时返回空。"""
    if mode == "all_days":
        return True
    holidays = _WORKDAY_HOLIDAYS if mode == "cn_workday" else _MARKET_HOLIDAYS
    if day.year not in holidays:
        return None
    if mode == "cn_workday" and day in _MAKEUP_WORKDAYS:
        return True
    return day.weekday() < 5 and day not in holidays[day.year]
