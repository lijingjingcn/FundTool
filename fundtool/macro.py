# -*- coding: utf-8 -*-
"""宏观数据客户端：中美国债收益率、中证指数估值（收盘/市盈率）、CPI/PPI/PMI/GDP/货币供应、美元兑离岸人民币。

数据来源均为公开免费接口，无需登录：
- 东方财富数据中心 datacenter-web.eastmoney.com（中美国债收益率、CPI/PPI/PMI/GDP/货币供应）
- 中证指数官网 www.csindex.com.cn（中证系列指数的日收盘与市盈率，2005 年起）
- 东方财富行情 push2（美元兑离岸人民币现价）

纯数据层，不依赖 streamlit。接口若失效集中改这里。
"""
import bisect
import time
import urllib.request
from datetime import datetime, timedelta

import requests

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# 股债性价比支持的指数（csindex 官网仅收录中证系列指数，创业板指等深证指数不在此列）
INDEXES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000906": "中证800",
    "000852": "中证1000",
    "000985": "中证全指",
}


def tr_code(code):
    """中证指数对应的全收益指数代码（000300 -> H00300）"""
    return "H" + code[1:]

_CSINDEX_START = "20050104"  # 指数估值历史起点（csindex 官网自 2005 年起提供市盈率）

# 中美国债收益率报表 RPTA_WEB_TREASURYYIELD 的字段为 EM 指标码，按下表映射
_BOND_FIELDS = {
    "EMM00588704": "cn2",      # 中国国债收益率 2 年期
    "EMM00166462": "cn5",      # 中国 5 年期
    "EMM00166466": "cn10",     # 中国 10 年期
    "EMM00166469": "cn30",     # 中国 30 年期
    "EMM01276014": "cn10y2y",  # 中国 10 年-2 年期限利差
    "EMG00001306": "us2",      # 美国 2 年期
    "EMG00001308": "us5",      # 美国 5 年期
    "EMG00001310": "us10",     # 美国 10 年期
    "EMG00001312": "us30",     # 美国 30 年期
    "EMG01339436": "us10y2y",  # 美国 10 年-2 年期限利差
}

# 宏观指标报表：报表名 / 拉取条数 / 行映射函数（东方财富数据中心，按 REPORT_DATE 倒序返回）
_ECON_REPORTS = {
    "cpi": ("RPT_ECONOMY_CPI", 180, lambda r: {"cpi": r.get("NATIONAL_SAME")}),
    "ppi": ("RPT_ECONOMY_PPI", 180, lambda r: {"ppi": r.get("BASE_SAME")}),
    "pmi": ("RPT_ECONOMY_PMI", 180, lambda r: {"pmi": r.get("MAKE_INDEX")}),
    "gdp": ("RPT_ECONOMY_GDP", 80, lambda r: {"gdp": r.get("SUM_SAME")}),
    "money": ("RPT_ECONOMY_CURRENCY_SUPPLY", 180, lambda r: {
        "m2": r.get("BASIC_CURRENCY_SAME"), "m1": r.get("CURRENCY_SAME"), "m0": r.get("FREE_CASH_SAME"),
    }),
}


class MacroApiError(Exception):
    pass


# ---------------- 纯函数（可离线测试） ----------------
def norm_date(s):
    """统一日期格式：'20260930' 或 '1990-12-19 00:00:00' -> '1990-12-19'"""
    s = str(s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def percentile_rank(values, x):
    """x 在 values 中的历史分位（0~100，取小于等于 x 的占比）；空序列返回 None"""
    vals = [v for v in (values or []) if v is not None]
    if not vals or x is None:
        return None
    return sum(1 for v in vals if v <= x) / len(vals) * 100


def erp_series(index_rows, bond_rows):
    """股债性价比序列：指数盈利收益率(100/PE) − 中国 10 年期国债收益率，单位百分点。

    index_rows 为 index_daily() 的返回（升序），bond_rows 为 bond_yield() 的返回；
    按日期精确对齐（两边都是交易日），缺 PE 或缺当日国债收益率的点直接跳过。
    """
    y10 = {r["date"]: r["cn10"] for r in bond_rows if r.get("cn10") is not None}
    out = []
    for r in index_rows:
        pe, d = r.get("pe"), r.get("date")
        if not pe or d not in y10:
            continue
        ep = 100.0 / pe
        out.append({"date": d, "close": r.get("close"), "pe": pe,
                    "ep": ep, "y10": y10[d], "erp": ep - y10[d]})
    return out


def dividend_yield_series(price_rows, tr_rows, lookback_days=365):
    """滚动 12 个月股息率序列（单位 %），由全收益指数与价格指数推导：

        股息率_t = TR_t × P_s / (TR_s × P_t) − 1，s 为约一年前最近的交易日

    全收益指数含分红再投资、价格指数不含，两者比值即期间的滚动分红收益率。
    与中证官网"股息率（计算用股本）"口径约有 0.1 个百分点的差异（再投资时点不同）。
    """
    p = {r["date"]: r.get("close") for r in price_rows if r.get("close")}
    rows = [r for r in tr_rows if r.get("close") and r["date"] in p]
    dates = [r["date"] for r in rows]
    out = []
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        cutoff = (d - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        k = bisect.bisect_left(dates, cutoff)
        if k <= 0:
            continue  # 回看期数据不足（序列开头）
        base = rows[k - 1]
        y = (r["close"] / base["close"]) * (p[base["date"]] / p[r["date"]]) - 1
        if -0.5 < y < 0.5:  # 排除数据异常点
            out.append({"date": r["date"], "dp": y * 100.0})
    return out


def bond_spread_series(value_rows, bond_rows, vkey):
    """任意指标值 − 中国 10 年期国债收益率的点差序列（百分点），按日期精确对齐。"""
    y10 = {r["date"]: r["cn10"] for r in bond_rows if r.get("cn10") is not None}
    out = []
    for r in value_rows:
        v, d = r.get(vkey), r.get("date")
        if v is None or d not in y10:
            continue
        out.append({"date": d, vkey: v, "y10": y10[d], "spread": v - y10[d]})
    return out


# ---------------- 客户端 ----------------
class MacroClient:
    def __init__(self, cache, rate_limit=0.35, timeout=30):
        self.cache = cache
        self.rate_limit = rate_limit
        self.timeout = timeout
        self.session = requests.Session()
        # 数据源均为国内站点，与 client.py 同理忽略系统代理，避免经境外节点被重置连接
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": _UA})
        self._last_req = 0.0

    def _throttle(self):
        wait = self.rate_limit - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _get(self, url, params=None, timeout=None):
        last_err = None
        for attempt in range(4):
            try:
                self._throttle()
                r = self.session.get(url, params=params, timeout=timeout or self.timeout)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.0 + 2.0 * attempt)
        raise MacroApiError(f"请求失败: {url} ({last_err})")

    # ---------- 中美国债收益率 ----------
    def bond_yield(self):
        """中美国债收益率历史（2005 年至今，升序）。行字段见 _BOND_FIELDS 映射，单位 %。

        接口 pageSize 上限 500（约两年），按页翻取直到覆盖 2005 年。
        """
        cached = self.cache.get("macro", "bond_yield", ttl=6 * 3600)
        if cached is not None:
            return cached
        merged = {}
        for page in range(1, 20):
            j = self._get(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={"columns": "ALL", "pageSize": 500, "pageNumber": page,
                        "reportName": "RPTA_WEB_TREASURYYIELD", "source": "WEB", "client": "WEB"},
                timeout=20,
            ).json()
            raw = (j.get("result") or {}).get("data") or []
            if not raw:
                break
            for r in raw:  # 接口按日期倒序返回
                item = {"date": norm_date(r.get("SOLAR_DATE"))}
                for src, dst in _BOND_FIELDS.items():
                    item[dst] = r.get(src)
                if item["cn10"] is not None or item["us10"] is not None:
                    merged[item["date"]] = item
            oldest = min(merged)
            if len(raw) < 500 or oldest <= "2004-12-31":
                break
        rows = [merged[d] for d in sorted(merged)]
        if not rows:
            raise MacroApiError("中美国债收益率接口未返回数据")
        self.cache.set("macro", "bond_yield", rows)
        return rows

    # ---------- 中证指数收盘与市盈率 ----------
    def index_daily(self, code):
        """指数日线收盘价与市盈率（csindex 官网口径），2005 年至今，升序。pe=None 的早期数据已跳过。"""
        cached = self.cache.get("macro", f"index_{code}", ttl=6 * 3600)
        if cached is not None:
            return cached
        today = time.strftime("%Y%m%d")
        r = self._get(
            "https://www.csindex.com.cn/csindex-home/perf/index-perf",
            params={"indexCode": code, "startDate": _CSINDEX_START, "endDate": today},
            timeout=30,
        )
        j = r.json()
        if j.get("code") != "200":
            raise MacroApiError(f"中证指数接口异常: {code} {j.get('msg')}")
        rows = [{"date": norm_date(d.get("tradeDate")), "close": d.get("close"), "pe": d.get("peg")}
                for d in j.get("data") or []]
        rows = [x for x in rows if x["pe"] is not None]
        if not rows:
            raise MacroApiError(f"指数 {code} 未返回估值数据")
        self.cache.set("macro", f"index_{code}", rows)
        return rows

    # ---------- 宏观月度/季度指标 ----------
    def econ(self, kind):
        """宏观指标历史（升序）。kind: cpi / ppi / pmi / gdp / money，行为 {month, label, **指标值}。"""
        if kind not in _ECON_REPORTS:
            raise MacroApiError(f"未知宏观指标: {kind}")
        cached = self.cache.get("macro", f"econ_{kind}", ttl=12 * 3600)
        if cached is not None:
            return cached
        report, size, mapper = _ECON_REPORTS[kind]
        j = self._get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={"columns": "ALL", "pageSize": size, "pageNumber": 1,
                    "reportName": report, "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                    "source": "WEB", "client": "WEB"},
            timeout=20,
        ).json()
        raw = (j.get("result") or {}).get("data") or []
        rows = []
        for r in raw:  # 接口按 REPORT_DATE 倒序返回（最新的 180/80 条），翻转为升序
            item = {"month": norm_date(r.get("REPORT_DATE"))[:7], "label": r.get("TIME")}
            item.update(mapper(r))
            rows.append(item)
        rows.reverse()
        if not rows:
            raise MacroApiError(f"宏观指标 {kind} 未返回数据")
        self.cache.set("macro", f"econ_{kind}", rows)
        return rows

    # ---------- 美元兑离岸人民币 ----------
    def usdcnh(self):
        """美元兑离岸人民币现价：{price, prev, chg_pct}（chg_pct 为较上日涨跌幅 %，人民币升值为负）"""
        cached = self.cache.get("macro", "usdcnh", ttl=1800)
        if cached is not None:
            return cached
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {"secid": "133.USDCNH", "fields": "f43,f60"}
        # push2 行情接口在部分网络环境下直连会被重置：直连只试一次（短超时），失败立即回退系统代理
        r = None
        self._throttle()
        try:
            r = self.session.get(url, params=params, timeout=8)
            r.raise_for_status()
        except requests.RequestException:
            self._throttle()
            try:
                r = self.session.get(url, params=params, timeout=15,
                                     proxies=urllib.request.getproxies())
                r.raise_for_status()
            except requests.RequestException as e:
                raise MacroApiError(f"请求失败: {url} ({e})")
        data = (r.json() or {}).get("data") or {}
        price, prev = data.get("f43"), data.get("f60")
        if not price:
            raise MacroApiError("美元兑离岸人民币接口未返回数据")
        out = {"price": price / 1e4, "prev": (prev or 0) / 1e4,
               "chg_pct": (price / prev - 1) * 100 if prev else None}
        self.cache.set("macro", "usdcnh", out)
        return out
