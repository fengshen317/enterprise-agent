"""基金行情连接器的结构化口径和只读工具契约。"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.connectors.catalog import get_builtin
from app.connectors.fund_market import SHANGHAI, FundMarketConnector
from app.core.config import Settings


async def test_fund_tools_are_free_read_only_and_validate_codes() -> None:
    """模板可通过内置目录安装，不需要密钥，工具拒绝无效代码和过长历史。"""
    builtin = get_builtin("fund_market")
    assert builtin is not None and builtin.kind == "api" and not builtin.config_fields
    connector = FundMarketConnector(Settings())
    tools = connector.tool_specs(await connector.list_tools(), {})
    assert {tool.key for tool in tools} == {
        "fund_market__etf_quote",
        "fund_market__fund_nav",
        "fund_market__fund_estimate",
    }
    assert all(tool.risk == "read" and tool.approval == "never" and tool.child_allowed for tool in tools)
    with pytest.raises(ValidationError):
        await connector.call("etf_quote", {"code": "600519"})
    with pytest.raises(ValidationError):
        await connector.call("fund_nav", {"code": 22})
    with pytest.raises(ValidationError):
        await connector.call("fund_nav", {"code": "000022", "limit": 31})


async def test_etf_quote_uses_trade_date_and_minute_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """分钟样本按实际时间排序，涨跌基准和最新分钟可以追溯。"""
    upstream = Mock(
        return_value=[
            {"时间": "2026-11-18 10:32:00", "收盘": 4.04},
            {"时间": "2026-11-17 15:00:00", "收盘": 4.0},
            {"时间": "2026-11-18 10:31:00", "收盘": "nan"},
            {"时间": "2026-11-18 10:30:00", "收盘": 4.01},
        ]
    )
    monkeypatch.setattr("app.connectors.fund_market._akshare_rows", upstream)
    connector = FundMarketConnector(Settings())
    first, second = await asyncio.gather(
        connector.call("etf_quote", {"code": "510300"}), connector.call("etf_quote", {"code": "510300"})
    )
    assert first == second
    assert isinstance(first, dict)
    assert first["data_kind"] == "etf_trade" and first["source"] == "AKShare / 东方财富"
    assert first["as_of"] == "2026-11-18T10:32:00+08:00" and first["price"] == 4.04
    assert first["previous_close_as_of"] == "2026-11-17T15:00:00+08:00"
    assert first["change_pct_vs_previous_close"] == 1.0
    assert [row["price"] for row in first["points"]] == [4.01, 4.04]
    upstream.assert_called_once_with("fund_etf_hist_min_em", {"symbol": "510300", "period": "1"})


async def test_nav_history_and_health_share_public_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    """健康检查无需密钥，净值输出保留真实日期并按请求裁剪。"""
    upstream = Mock(
        return_value=[
            {"净值日期": "2026-11-17", "单位净值": 3.027, "日增长率": "0.63%"},
            {"净值日期": "2026-11-16", "单位净值": 3.008, "日增长率": "---"},
            {"净值日期": "2026-11-18", "单位净值": float("nan"), "日增长率": "---"},
        ]
    )
    monkeypatch.setattr("app.connectors.fund_market._akshare_rows", upstream)
    connector = FundMarketConnector(Settings())
    health = await connector.check_health()
    assert health.status == "connected" and health.auth_status == "not_required"
    result = await connector.call("fund_nav", {"code": "110022", "limit": 1})
    assert result == {
        "code": "110022",
        "data_kind": "published_nav",
        "source": "AKShare / 东方财富",
        "status": "available",
        "as_of": "2026-11-17",
        "points": [{"date": "2026-11-17", "unit_nav": 3.027, "change_pct": 0.63}],
    }
    upstream.assert_called_once()


async def test_estimate_does_not_fill_missing_codes_or_stale_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享当日估值表，但不会把旧数据或其他代码的估值返回给当前标的。"""
    today = datetime.now(SHANGHAI).date()
    old = today - timedelta(days=1)
    upstream = Mock(
        return_value=[
            {"基金代码": "000022", f"{today}-估算数据-估算值": "1.24", f"{today}-估算数据-估算增长率": "-0.6%"},
            {"基金代码": "110022", f"{old}-估算数据-估算值": "3.14", f"{today}-公布数据-单位净值": "3.04"},
        ]
    )
    monkeypatch.setattr("app.connectors.fund_market._akshare_rows", upstream)
    connector = FundMarketConnector(Settings())
    available = await connector.call("fund_estimate", {"code": "000022"})
    assert available == {
        "code": "000022",
        "data_kind": "estimated_nav",
        "source": "AKShare / 东方财富",
        "status": "available",
        "as_of": today.isoformat(),
        "time_precision": "date",
        "estimated_nav": 1.24,
        "estimated_change_pct": -0.6,
    }
    for code in ("110022", "000001"):
        missing = await connector.call("fund_estimate", {"code": code})
        assert isinstance(missing, dict) and missing["status"] == "unavailable" and missing["as_of"] is None
        assert "estimated_nav" not in missing
    upstream.assert_called_once_with("fund_value_estimation_em", {"symbol": "全部"})
