"""只读基金行情连接器，使用 AKShare 查询公开行情。"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from datetime import date, datetime
from importlib import import_module
from time import monotonic
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.capabilities.spec import ToolContext, ToolSpec
from app.connectors.base import (
    ConfigField,
    ConnectorError,
    ConnectorKind,
    ConnectorStatusInfo,
    ConnectorToolDef,
    ToolPolicyOverride,
    health_exception,
    make_tool_key,
)
from app.connectors.catalog import FUND_MARKET

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.core.config import Settings

type Rows = list[dict[str, object]]

SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE = "AKShare / 东方财富"
HEALTH_SAMPLE = "110022"
MAX_NAV_POINTS = 30
MAX_ETF_POINTS = 15
MAX_INFLIGHT = 4
MAX_CACHE_ENTRIES = 64
QUERY_TIMEOUT_SECONDS = 12
ETF_CACHE_SECONDS = 20
NAV_CACHE_SECONDS = 300
ESTIMATE_CACHE_SECONDS = 60


class FundCodeArgs(BaseModel):
    """基金代码须使用字符串，以保留前导零。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=r"^[0-9]{6}$", description="六位场外基金代码，如 110022")


class EtfQuoteArgs(FundCodeArgs):
    """沪深 ETF 分时行情使用场内代码。"""

    code: str = Field(pattern=r"^(?:159|5[123568][0-9])[0-9]{3}$", description="沪深 ETF 代码，如 510300 或 159915")


class FundNavArgs(FundCodeArgs):
    """限制净值历史长度，避免把完整数据表送入模型。"""

    limit: int = Field(default=10, ge=1, le=MAX_NAV_POINTS, description="返回最近的净值记录条数")


TOOLS: dict[str, tuple[str, type[FundCodeArgs]]] = {
    "etf_quote": ("查询沪深场内 ETF 最新一分钟成交行情与近期分钟走势；以返回的 as_of 为准。", EtfQuoteArgs),
    "fund_nav": ("查询场外基金最近已披露单位净值和历史走势；以净值日期为准。", FundNavArgs),
    "fund_estimate": (
        "查询免费源提供的场外基金当日参考估值。估值不是披露净值；返回 unavailable 时请说明暂无估值。",
        FundCodeArgs,
    ),
}


def _akshare_rows(method: str, arguments: dict[str, str]) -> Rows:
    """调用同步 SDK，只将行记录带回异步连接器。

    Args:
        method: 内置的数据函数名。
        arguments: 已校验的 SDK 参数。

    Returns:
        原始行情行记录。

    Raises:
        ConnectorError: 上游调用失败或返回表格异常。
    """
    try:
        sdk = import_module("akshare")
        frame = getattr(sdk, method)(**arguments)
        return cast(Rows, frame.to_dict("records"))
    except ImportError as exc:
        raise ConnectorError("not_configured", "未安装基金行情依赖，请同步后端依赖") from exc
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ConnectorError("invalid_output", "免费行情源未返回可识别的数据，请核对基金代码后重试") from exc
    except Exception as exc:
        raise ConnectorError("network_error", "免费行情源暂时不可用，请稍后重试") from exc


def _number(value: object) -> float | None:
    """把 SDK 数值和百分数字符串转换为有限数值，缺失值保留为空。"""
    try:
        number = float(str(value).strip().removesuffix("%"))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _nav_points(rows: Rows) -> Rows:
    """按净值日期整理有效样本，避免上游顺序或空值影响最新净值。"""
    points: dict[str, dict[str, object]] = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("净值日期"))).isoformat()
        except ValueError:
            continue
        value = _number(row.get("单位净值"))
        if value is not None and value > 0:
            points[day] = {"date": day, "unit_nav": value, "change_pct": _number(row.get("日增长率"))}
    return [points[day] for day in sorted(points)]


def _etf_quote(rows: Rows) -> dict[str, object]:
    """从分钟线提取最近行情日及可用的上一行情日收盘参考。"""
    points: dict[datetime, float] = {}
    for row in rows:
        raw_time = str(row.get("时间"))
        price = _number(row.get("收盘"))
        if price is None or price <= 0 or ":" not in raw_time:
            continue
        try:
            stamp = datetime.fromisoformat(raw_time)
        except ValueError:
            continue
        stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)
        points[stamp] = price
    if not points:
        return {"status": "unavailable", "as_of": None, "message": "暂无 ETF 分钟行情"}

    stamps = sorted(points)
    latest = stamps[-1]
    today = [stamp for stamp in stamps if stamp.date() == latest.date()]
    previous = next((stamp for stamp in reversed(stamps) if stamp.date() < latest.date()), None)
    prev_close = points[previous] if previous is not None else None
    return {
        "status": "available",
        "as_of": latest.isoformat(),
        "price": points[latest],
        "change_pct_vs_previous_close": round((points[latest] / prev_close - 1) * 100, 4) if prev_close else None,
        "previous_close_as_of": previous.isoformat() if previous is not None else None,
        "points": [{"as_of": stamp.isoformat(), "price": points[stamp]} for stamp in today[-MAX_ETF_POINTS:]],
    }


def _fund_estimate(rows: Rows, code: str, today: date) -> dict[str, object]:
    """只接受带当天日期的估值列，历史净值和旧估值均不可充当当前估值。"""
    day = today.isoformat()
    row = next((item for item in rows if str(item.get("基金代码")) == code), {})
    value = _number(row.get(f"{day}-估算数据-估算值"))
    if value is None or value <= 0:
        return {"status": "unavailable", "as_of": None, "message": "暂无当日盘中估值，可另查已披露净值"}
    return {
        "status": "available",
        "as_of": day,
        "time_precision": "date",
        "estimated_nav": value,
        "estimated_change_pct": _number(row.get(f"{day}-估算数据-估算增长率")),
    }


class FundMarketConnector:
    """以 API 类别展示内置数据适配器，运行在后端进程内。"""

    kind: ConnectorKind = "api"
    key = FUND_MARKET.key

    def __init__(self, settings: Settings) -> None:
        """保存时限配置和实例级公开数据缓存。"""
        self._settings = settings
        self._cache: OrderedDict[tuple[str, str], tuple[float, Rows]] = OrderedDict()
        self._pending: dict[tuple[str, str], asyncio.Task[Rows]] = {}

    def config_fields(self) -> tuple[ConfigField, ...]:
        """返回免费公开数据源的空配置表单。"""
        return ()

    async def connect(self) -> ConnectorStatusInfo:
        """声明免认证，真实上游探测交给 check_health。"""
        return ConnectorStatusInfo(status="connected", auth_status="not_required")

    async def check_health(self) -> ConnectorStatusInfo:
        """通过公开净值样本检查上游，复用同一个短期缓存。"""
        try:
            if not _nav_points(await self._nav(HEALTH_SAMPLE)):
                raise ConnectorError("invalid_output", "未获取到探测基金的净值")
        except (ConnectorError, TimeoutError) as exc:
            return health_exception(exc)
        return ConnectorStatusInfo(status="connected", auth_status="not_required")

    async def list_tools(self) -> list[ConnectorToolDef]:
        """发现基金行情的三个只读工具。"""
        return [
            ConnectorToolDef(name, description, model.model_json_schema(), risk="read", approval="never")
            for name, (description, model) in TOOLS.items()
        ]

    def tool_specs(self, defs: list[ConnectorToolDef], policy: Mapping[str, ToolPolicyOverride]) -> list[ToolSpec]:
        """保留强类型参数校验，接入问答的权限、审计与配额。"""
        specs = []
        for definition in defs:
            name = definition.name
            override = policy.get(name)
            risk = override.risk if override else definition.risk
            approval = override.approval if override else definition.approval

            async def handler(context: ToolContext, arguments: BaseModel, tool_name: str = name) -> object:
                """调用固定行情函数，查询上下文不传给外部数据源。"""
                return await self.call(tool_name, arguments.model_dump())

            def audit(arguments: BaseModel, result: object) -> dict[str, object]:
                """记录查询结果状态，不把自选代码和历史净值写入审计。"""
                return {"status": result.get("status")} if isinstance(result, dict) else {}

            specs.append(
                ToolSpec(
                    key=make_tool_key(self.key, name),
                    version=1,
                    display_name=name,
                    description=definition.description,
                    args_model=TOOLS[name][1],
                    risk=risk,
                    approval=approval,
                    timeout_seconds=self._settings.tool_timeout_seconds + QUERY_TIMEOUT_SECONDS,
                    quota_per_run=5,
                    child_allowed=risk == "read",
                    handler=handler,
                    audit=audit,
                    source="connector",
                    connector_key=self.key,
                )
            )
        return specs

    async def call(self, name: str, arguments: dict[str, object]) -> object:
        """执行已注册的查询并返回带来源和数据类型的结构化结果。"""
        if name not in TOOLS:
            raise ConnectorError("command_failed", "未知基金行情工具")
        args = TOOLS[name][1].model_validate(arguments)
        if name == "etf_quote":
            rows = await self._load("fund_etf_hist_min_em", args.code, ETF_CACHE_SECONDS, symbol=args.code, period="1")
            result = _etf_quote(rows)
            data_kind = "etf_trade"
        elif isinstance(args, FundNavArgs):
            points = _nav_points(await self._nav(args.code))[-args.limit :]
            result = {
                "status": "available" if points else "unavailable",
                "as_of": points[-1]["date"] if points else None,
                "points": points,
            }
            data_kind = "published_nav"
        else:
            rows = await self._load("fund_value_estimation_em", "", ESTIMATE_CACHE_SECONDS, symbol="全部")
            result = _fund_estimate(rows, args.code, datetime.now(SHANGHAI).date())
            data_kind = "estimated_nav"
        return {"code": args.code, "data_kind": data_kind, "source": SOURCE, **result}

    async def _nav(self, code: str) -> Rows:
        """共享同一基金的历史净值，按调用方需求裁剪记录。"""
        return await self._load(
            "fund_open_fund_info_em", code, NAV_CACHE_SECONDS, symbol=code, indicator="单位净值走势"
        )

    async def _load(self, method: str, code: str, ttl: float, **arguments: str) -> Rows:
        """合并相同请求并限制并发，SDK 阻塞等待不占用事件循环。"""
        key = (method, code)
        entry = self._cache.get(key)
        if entry is not None and monotonic() < entry[0]:
            self._cache.move_to_end(key)
            return entry[1]
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= MAX_INFLIGHT:
                raise ConnectorError("timeout", "基金行情请求较多，请稍后重试")

            async def fetch() -> Rows:
                """仅缓存成功完成的请求，并限制缓存条目总数。"""
                rows = await asyncio.to_thread(_akshare_rows, method, arguments)
                self._cache[key] = (monotonic() + ttl, rows)
                self._cache.move_to_end(key)
                if len(self._cache) > MAX_CACHE_ENTRIES:
                    self._cache.popitem(last=False)
                return rows

            def complete(done: asyncio.Task[Rows]) -> None:
                """调用方超时后也回收任务，并消费迟到的异常。"""
                self._pending.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task = asyncio.create_task(fetch())
            self._pending[key] = task
            task.add_done_callback(complete)

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=QUERY_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ConnectorError("timeout", "免费行情源响应超时，请稍后重试") from exc

    async def aclose(self) -> None:
        """停用后撤销异步等待并清除实例缓存。"""
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()
