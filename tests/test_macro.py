# -*- coding: utf-8 -*-
"""宏观数据层纯函数单测（完全离线，不访问网络）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fundtool.macro import (  # noqa: E402
    INDEXES,
    MacroApiError,
    bond_spread_series,
    dividend_yield_series,
    erp_series,
    norm_date,
    percentile_rank,
    tr_code,
)


# ---------------- norm_date ----------------
def test_norm_date_compact():
    assert norm_date("20260930") == "2026-09-30"


def test_norm_date_datetime():
    assert norm_date("1990-12-19 00:00:00") == "1990-12-19"
    assert norm_date("2026-09-30") == "2026-09-30"


def test_norm_date_empty():
    assert norm_date(None) == ""
    assert norm_date("") == ""


# ---------------- percentile_rank ----------------
def test_percentile_basic():
    assert percentile_rank([1, 2, 3, 4, 5], 3) == 60.0


def test_percentile_ties_and_edges():
    assert percentile_rank([2, 2, 2], 2) == 100.0
    assert percentile_rank([1, 2, 3], 0) == 0.0
    assert percentile_rank([1, 2, 3], 9) == 100.0


def test_percentile_empty_or_none():
    assert percentile_rank([], 1) is None
    assert percentile_rank(None, 1) is None
    assert percentile_rank([1, 2], None) is None


def test_percentile_skips_none_values():
    assert percentile_rank([None, 1, 2, 3, None, 4], 2) == 50.0


# ---------------- erp_series ----------------
def _index_rows():
    return [
        {"date": "2026-09-28", "close": 4400.0, "pe": 13.0},
        {"date": "2026-09-29", "close": 4450.0, "pe": 13.5},
        {"date": "2026-09-30", "close": 4500.0, "pe": None},   # 缺 PE 应跳过
        {"date": "2026-10-01", "close": 4510.0, "pe": 12.5},   # 当日无国债收益率应跳过
    ]


def _bond_rows():
    return [
        {"date": "2026-09-28", "cn10": 1.7},
        {"date": "2026-09-29", "cn10": None},  # 缺 10Y 应跳过
        {"date": "2026-09-30", "cn10": 1.6},
        {"date": "2026-10-02", "cn10": 1.6},
    ]


def test_erp_series_merge_and_math():
    out = erp_series(_index_rows(), _bond_rows())
    assert [r["date"] for r in out] == ["2026-09-28"]
    r = out[0]
    assert abs(r["ep"] - 100.0 / 13.0) < 1e-9
    assert abs(r["erp"] - (100.0 / 13.0 - 1.7)) < 1e-9
    assert r["y10"] == 1.7 and r["close"] == 4400.0


def test_erp_series_empty_inputs():
    assert erp_series([], _bond_rows()) == []
    assert erp_series(_index_rows(), []) == []


def test_erp_series_ascending_order_preserved():
    idx = [{"date": d, "close": 1.0, "pe": 10.0} for d in ("2026-01-05", "2026-01-06", "2026-01-07")]
    bond = [{"date": d, "cn10": 2.0} for d in ("2026-01-05", "2026-01-06", "2026-01-07")]
    out = erp_series(idx, bond)
    assert [r["erp"] for r in out] == [8.0, 8.0, 8.0]


# ---------------- 目录与异常 ----------------
def test_indexes_catalog_and_tr_code():
    assert INDEXES["000300"] == "沪深300"
    assert "399006" not in INDEXES  # 深证指数不在中证官网，不应收录
    assert tr_code("000300") == "H00300"
    assert tr_code("000985") == "H00985"


# ---------------- dividend_yield_series（全收益/价格指数推导股息率） ----------------
def _annual_div_rows(rate=0.024):
    """价格指数恒为 100，全收益指数按年化 rate 复利增长（日历日逐日），覆盖 2025-01-01~2026-06-30"""
    from datetime import date as _d, timedelta as _td
    start, end = _d(2025, 1, 1), _d(2026, 6, 30)
    price, tr = [], []
    d = start
    while d <= end:
        s = (d - start).days
        price.append({"date": d.isoformat(), "close": 100.0})
        tr.append({"date": d.isoformat(), "close": 100.0 * (1 + rate) ** (s / 365.0)})
        d += _td(days=1)
    return price, tr


def test_dividend_yield_matches_annual_rate():
    out = dividend_yield_series(*_annual_div_rows())
    assert out, "一年回看期之后应有输出"
    assert all(r["date"] > "2026-01-01" for r in out), "首年数据不足应被跳过"
    assert all(2.2 < r["dp"] < 2.6 for r in out[-20:]), [r["dp"] for r in out[-20:]]


def test_dividend_yield_zero_when_no_dividend():
    price, tr = _annual_div_rows(rate=0.0)
    tr = [{"date": r["date"], "close": 100.0} for r in tr]
    out = dividend_yield_series(price, tr)
    assert all(abs(r["dp"]) < 0.01 for r in out)


def test_dividend_yield_drops_outliers_and_missing_price():
    price, tr = _annual_div_rows()
    tr[-1]["close"] = 50.0  # 异常腰斩 → 该点被过滤
    price[10]["close"] = None  # 缺价格的日期被跳过
    out = dividend_yield_series(price, tr)
    assert all(-5 < r["dp"] < 5 for r in out)
    assert out[-1]["date"] < tr[-1]["date"]


# ---------------- bond_spread_series ----------------
def test_bond_spread_merge_and_math():
    values = [{"date": "2026-09-28", "dp": 2.4}, {"date": "2026-09-29", "dp": 2.5},
              {"date": "2026-10-01", "dp": 2.6}]  # 10-01 无国债数据应跳过
    bond = [{"date": "2026-09-28", "cn10": 1.7}, {"date": "2026-09-29", "cn10": None},
            {"date": "2026-09-30", "cn10": 1.6}]
    out = bond_spread_series(values, bond, "dp")
    assert [r["date"] for r in out] == ["2026-09-28"]
    assert abs(out[0]["spread"] - 0.7) < 1e-9
    assert out[0]["y10"] == 1.7 and out[0]["dp"] == 2.4


def test_bond_spread_empty_inputs():
    assert bond_spread_series([], [{"date": "d", "cn10": 1.0}], "dp") == []
    assert bond_spread_series([{"date": "d", "dp": 1.0}], [], "dp") == []


def test_unknown_econ_kind_raises():
    from fundtool.macro import MacroClient
    client = MacroClient.__new__(MacroClient)  # 不触发网络/缓存构造
    try:
        client.econ("nope")
        assert False, "应抛出 MacroApiError"
    except MacroApiError:
        pass
