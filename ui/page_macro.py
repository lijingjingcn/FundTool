# -*- coding: utf-8 -*-
"""宏观指标页：股债性价比（ERP）、利率与汇率、增长·通胀·流动性。

数据：中证指数官网（指数收盘与市盈率）、东方财富（中美国债收益率、CPI/PPI/PMI/GDP/货币供应、
美元兑离岸人民币）。数据层见 fundtool/macro.py，本页只做取数编排与展示。
"""
import os
import sys
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fundtool.macro import (  # noqa: E402
    INDEXES,
    MacroApiError,
    bond_spread_series,
    dividend_yield_series,
    erp_series,
    percentile_rank,
    tr_code,
)
from ui.constants import ERP_HIGH_PCT, ERP_LOW_PCT  # noqa: E402
from ui.state import get_macro  # noqa: E402

alt.data_transformers.disable_max_rows()  # 指数估值全历史超过 altair 默认行数上限

st.title("🌏 宏观指标")
st.caption("股债性价比、利率汇率与增长通胀流动性一览。数据来自中证指数官网与东方财富公开接口，每日更新，仅供参考。")

# ---------------- 取数（各来源独立容错，部分失败不影响其他板块） ----------------
macro = get_macro()
bond, idx_hist, econ, fx = None, {}, {}, None
problems = []
with st.spinner("拉取宏观数据（首次约需十几秒，之后走本地缓存）"):
    try:
        bond = macro.bond_yield()
    except MacroApiError as e:
        problems.append(f"国债收益率：{e}")
    for _code in INDEXES:
        try:
            idx_hist[_code] = macro.index_daily(_code)
        except MacroApiError as e:
            problems.append(f"{INDEXES[_code]}：{e}")
    tr_hist = {}
    for _code in INDEXES:
        try:
            tr_hist[_code] = macro.index_daily(tr_code(_code))
        except MacroApiError:
            tr_hist[_code] = None  # 全收益指数缺失时股息率口径降级，不影响其他板块
    for _kind in ("cpi", "ppi", "pmi", "gdp", "money"):
        try:
            econ[_kind] = macro.econ(_kind)
        except MacroApiError as e:
            problems.append(f"{_kind}：{e}")
    try:
        fx = macro.usdcnh()
    except MacroApiError as e:
        problems.append(f"汇率：{e}")
if problems:
    st.warning("部分数据拉取失败（可稍后刷新重试）：" + "；".join(problems[:3]) + ("……" if len(problems) > 3 else ""))

erps = {c: (erp_series(rows, bond) if bond else []) for c, rows in idx_hist.items()}
# 股息率口径：全收益/价格指数推导滚动12M股息率，再与10Y国债求点差
dy_spreads = {}
for _c, _rows in idx_hist.items():
    _tr = tr_hist.get(_c)
    if bond and _tr:
        dy_spreads[_c] = bond_spread_series(dividend_yield_series(_rows, _tr), bond, "dp")
    else:
        dy_spreads[_c] = []


# ---------------- 小工具 ----------------
def _pct(v, nd=2):
    return "–" if v is None else f"{v:.{nd}f}%"


def _num_txt(v, nd=1):
    return "–" if v is None else f"{v:.{nd}f}"


def _latest_vals(rows, key):
    """某指标的 (最新值, 上一期值)，跳过缺值的期"""
    vals = [r.get(key) for r in (rows or []) if r.get(key) is not None]
    if not vals:
        return None, None
    return vals[-1], (vals[-2] if len(vals) > 1 else None)


def _bond_delta(field, back=30):
    """国债收益率较约一个月前（back 个交易日）的变化"""
    if not bond:
        return None
    vals = [r.get(field) for r in bond if r.get(field) is not None]
    if len(vals) < 2:
        return None
    return vals[-1] - (vals[-1 - back] if len(vals) > back else vals[0])


def _erp_hint(pct):
    if pct is None:
        return "–"
    if pct >= ERP_HIGH_PCT:
        return "📈 股票明显占优"
    if pct <= ERP_LOW_PCT:
        return "📉 债券明显占优"
    return "➖ 中性区间"


tab_erp, tab_rate, tab_econ = st.tabs(["⚖️ 股债性价比", "💰 利率与汇率", "📈 增长·通胀·流动性"])

# ================================================================ 股债性价比
with tab_erp:
    st.caption("股债性价比 = 指数盈利收益率(1÷PE) − 中国10年期国债收益率，越高代表股票相对债券越便宜；"
               "分位数按全部可得历史计算（指数估值自 2011 年中、国债收益率自 2005 年起）。")
    metric = st.radio("口径", ["盈利收益率 (1/PE)", "股息率"], horizontal=True, key="erp_metric")
    if metric == "股息率":
        st.caption("股息率口径 = 指数滚动12个月股息率 − 10年期国债收益率；股息率由中证官网全收益指数与价格指数推导"
                   "（与官方股息率口径约有 0.1 个百分点差异），为正说明仅分红收益就已高于国债。")
    if not bond or not erps:
        st.info("指数估值或国债收益率数据暂不可用，股债性价比无法计算。")
    else:
        if metric == "盈利收益率 (1/PE)":
            rows = []
            for code in INDEXES:
                if code not in erps or not erps[code]:
                    continue
                cur = erps[code][-1]
                rows.append({
                    "指数": f"{INDEXES[code]}（{code}）",
                    "PE(TTM)": cur["pe"],
                    "PE分位": percentile_rank([r["pe"] for r in erps[code]], cur["pe"]),
                    "盈利收益率": cur["ep"],
                    "股债性价比": cur["erp"],
                    "性价比分位": percentile_rank([r["erp"] for r in erps[code]], cur["erp"]),
                    "解读": _erp_hint(percentile_rank([r["erp"] for r in erps[code]], cur["erp"])),
                })
            col_fmt = {
                "PE(TTM)": st.column_config.NumberColumn(format="%.2f"),
                "PE分位": st.column_config.NumberColumn(format="%.1f%%"),
                "盈利收益率": st.column_config.NumberColumn(format="%.2f%%"),
                "股债性价比": st.column_config.NumberColumn(format="%.2f"),
                "性价比分位": st.column_config.NumberColumn(format="%.1f%%"),
            }
        else:
            rows = []
            for code in INDEXES:
                if code not in dy_spreads or not dy_spreads[code]:
                    continue
                cur = dy_spreads[code][-1]
                pct = percentile_rank([r["spread"] for r in dy_spreads[code]], cur["spread"])
                rows.append({
                    "指数": f"{INDEXES[code]}（{code}）",
                    "股息率": cur["dp"],
                    "10Y国债": cur["y10"],
                    "股息率性价比": cur["spread"],
                    "历史分位": pct,
                    "解读": _erp_hint(pct),
                })
            col_fmt = {
                "股息率": st.column_config.NumberColumn(format="%.2f%%"),
                "10Y国债": st.column_config.NumberColumn(format="%.2f%%"),
                "股息率性价比": st.column_config.NumberColumn(format="%.2f"),
                "历史分位": st.column_config.NumberColumn(format="%.1f%%"),
            }
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, column_config=col_fmt)
        elif metric == "股息率":
            st.info("全收益指数数据暂不可用，股息率口径无法计算。")

        sel = st.selectbox("图表指数", options=list(erps) or list(INDEXES),
                           format_func=lambda c: INDEXES.get(c, c))
        win_years = st.radio("历史窗口", ["全部", "近10年", "近5年"], horizontal=True, key="erp_win")
        show_close = st.toggle("叠加指数点位（右轴）", value=True, key="erp_show_close")

        series = erps[sel] if metric == "盈利收益率 (1/PE)" else dy_spreads.get(sel) or []
        if series:
            years = {"全部": None, "近10年": 10, "近5年": 5}[win_years]
            cutoff = (date.today() - timedelta(days=365 * years + 1)).isoformat() if years else None
            shown = [r for r in series if not cutoff or r["date"] >= cutoff]
            cur = series[-1]
            if metric == "盈利收益率 (1/PE)":
                cur_txt = f"PE {cur['pe']:.2f}，股债性价比 {cur['erp']:.2f}，历史分位 " \
                          f"{percentile_rank([r['erp'] for r in series], cur['erp']):.1f}%"
                vkey, y_title = "erp", "股债性价比（百分点）"
            else:
                cur_txt = f"股息率 {cur['dp']:.2f}%，减 10Y 国债 {cur['y10']:.2f}% = {cur['spread']:.2f}，" \
                          f"历史分位 {percentile_rank([r['spread'] for r in series], cur['spread']):.1f}%"
                vkey, y_title = "spread", "股息率 − 10Y国债（百分点）"
            st.caption(f"**{INDEXES[sel]} 当前**：{cur_txt}（{cur['date']}）")
            close_map = {r["date"]: r.get("close") for r in idx_hist.get(sel) or []}
            df = pd.DataFrame({"date": pd.to_datetime([r["date"] for r in shown]),
                               vkey: [r[vkey] for r in shown],
                               "close": [close_map.get(r["date"]) for r in shown]})
            mean = df[vkey].mean()
            std = df[vkey].std()

            def _rule(y, color, dash):
                return alt.Chart(pd.DataFrame({vkey: [y]})).mark_rule(
                    color=color, strokeDash=dash).encode(y=f"{vkey}:Q")

            line = alt.Chart(df).mark_line(color="#1f77b4").encode(
                x=alt.X("date:T", axis=alt.Axis(format="%Y-%m", title=None)),
                y=alt.Y(f"{vkey}:Q", title=y_title, scale=alt.Scale(zero=False),
                        axis=alt.Axis(orient="left")),
                tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                         alt.Tooltip(f"{vkey}:Q", title=y_title, format=".2f")],
            )
            chart = (line + _rule(mean, "#ff7f0e", [6, 3])
                     + _rule(mean + std, "#d9d9d9", [2, 3]) + _rule(mean - std, "#d9d9d9", [2, 3]))
            if show_close:
                close_line = alt.Chart(df).mark_line(color="#b0b0b0", size=1).encode(
                    # x 与主图层同字段共享同一根轴；此处不可设 axis=None，否则合并后整图 x 轴被隐藏
                    x=alt.X("date:T"),
                    y=alt.Y("close:Q", title="指数点位", scale=alt.Scale(zero=False),
                            axis=alt.Axis(orient="right")),
                    tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"),
                             alt.Tooltip("close:Q", title="点位", format=".2f")],
                )
                chart = alt.layer(close_line, chart).resolve_scale(y="independent")
            st.altair_chart(chart.properties(height=360), width="stretch")
            st.caption(f"橙色虚线为区间均值 {mean:.2f}，灰色虚线为 ±1σ（{mean - std:.2f} ~ {mean + std:.2f}），按所选窗口计算。"
                       + ("浅灰线为指数点位（右轴）。" if show_close else ""))

# ================================================================ 利率与汇率
with tab_rate:
    if not bond:
        st.info("国债收益率数据暂不可用。")
    else:
        cn10, _ = _latest_vals(bond, "cn10")
        cn30, _ = _latest_vals(bond, "cn30")
        us10, _ = _latest_vals(bond, "us10")
        spread10y2y, _ = _latest_vals(bond, "cn10y2y")
        both = [r for r in bond if r.get("cn10") is not None and r.get("us10") is not None]
        cn_us = (both[-1]["cn10"] - both[-1]["us10"]) if both else None
        d_cn10, d_us10 = _bond_delta("cn10"), _bond_delta("us10")

        m1, m2, m3 = st.columns(3)
        m1.metric("中国10年期国债", _pct(cn10), f"{d_cn10:+.2f}" if d_cn10 is not None else None,
                  help="较约一个月前变化（百分点）")
        m2.metric("中国30年期国债", _pct(cn30), f"{_bond_delta('cn30'):+.2f}" if _bond_delta("cn30") is not None else None,
                  help="较约一个月前变化（百分点）")
        m3.metric("中国期限利差 10Y−2Y", f"{spread10y2y:.2f}" if spread10y2y is not None else "–",
                  help="10年期减2年期国债收益率，反映对经济与通胀的预期")
        m4, m5, m6 = st.columns(3)
        m4.metric("美国10年期国债", _pct(us10), f"{d_us10:+.2f}" if d_us10 is not None else None,
                  help="较约一个月前变化（百分点）")
        m5.metric("中美10年期利差", f"{cn_us:+.2f}" if cn_us is not None else "–",
                  help="中国10年期 − 美国10年期，负值代表中美利率倒挂")
        if fx:
            m6.metric("美元兑离岸人民币", f"{fx['price']:.4f}",
                      f"{fx['chg_pct']:+.2f}%" if fx.get("chg_pct") is not None else None,
                      help="较上日涨跌幅，数值上涨代表人民币贬值")
        else:
            m6.metric("美元兑离岸人民币", "–")

        cutoff = (date.today() - timedelta(days=365 * 5)).isoformat()
        df = pd.DataFrame(
            [[r["date"], r.get("cn10"), r.get("us10")] for r in bond if r["date"] >= cutoff],
            columns=["date", "中国10年期", "美国10年期"],
        )
        df["date"] = pd.to_datetime(df["date"])
        st.caption("中美国债收益率走势（近5年，%）")
        st.line_chart(df.set_index("date"), height=320, width="stretch")

# ================================================================ 增长·通胀·流动性
with tab_econ:
    def _econ_metric(col, rows, key, label, kind="pct", help_txt=None):
        cur, prev = _latest_vals(rows, key)
        latest_month = next((r["month"] for r in reversed(rows or []) if r.get(key) is not None), "")
        delta = None if cur is None or prev is None else cur - prev
        fmt = (lambda v: f"{v:.1f}") if kind == "num" else (lambda v: _pct(v, 1))
        col.metric(f"{label} · {latest_month}", fmt(cur),
                   None if delta is None else f"{delta:+.1f}", help=help_txt)

    c1, c2, c3 = st.columns(3)
    _econ_metric(c1, econ.get("gdp"), "gdp", "GDP累计同比")
    _econ_metric(c2, econ.get("cpi"), "cpi", "CPI同比")
    _econ_metric(c3, econ.get("ppi"), "ppi", "PPI同比")
    c4, c5, c6 = st.columns(3)
    _econ_metric(c4, econ.get("pmi"), "pmi", "制造业PMI", kind="num", help_txt="高于 50 为扩张区间")
    _econ_metric(c5, econ.get("money"), "m2", "M2同比")
    _econ_metric(c6, econ.get("money"), "m1", "M1同比")

    # 最近 18 个月的月度明细（GDP 为季度数据，只在上面的指标卡展示）
    months = {}
    for kind, keys in (("cpi", ("cpi",)), ("ppi", ("ppi",)), ("pmi", ("pmi",)), ("money", ("m2", "m1"))):
        for r in econ.get(kind) or []:
            slot = months.setdefault(r["month"], {})
            for k in keys:
                if r.get(k) is not None:
                    slot[k] = r[k]
    table = [{"月份": m,
              "CPI同比": _pct(months[m].get("cpi"), 1),
              "PPI同比": _pct(months[m].get("ppi"), 1),
              "制造业PMI": _num_txt(months[m].get("pmi"), 1),
              "M2同比": _pct(months[m].get("m2"), 1),
              "M1同比": _pct(months[m].get("m1"), 1)}
             for m in sorted(months, reverse=True)[:18]]
    if table:
        st.caption("最近 18 个月明细")
        st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True)

# ---------------- 口径说明 ----------------
with st.expander("数据口径说明"):
    st.markdown(
        "- **股债性价比**：指数盈利收益率（1÷市盈率）− 中国10年期国债收益率，单位百分点；"
        "市盈率为中证指数官网发布的指数估值口径（TTM，2011 年中起提供），国债收益率自 2005 年起。\n"
        "- **国债收益率**：东方财富数据中心转发的中美国债到期收益率。\n"
        "- **CPI/PPI/PMI/GDP/M2/M1**：国家统计局/央行口径，东方财富数据中心转发；"
        "PMI 为制造业 PMI，M1/M2 为同比增速。\n"
        "- 免责声明：均为公开渠道数据，可能存在滞后或修正，不构成投资建议。"
    )
