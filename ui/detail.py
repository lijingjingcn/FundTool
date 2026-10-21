# -*- coding: utf-8 -*-
"""单只基金详情渲染：metric 卡、信息列、费率块、重仓股估值块、持有块、任职表。

供三处共用：分组查询每只基金的 expander、单只查询页底部（render_fund_detail_section）、
经理视图的就地详情。展示格式化取自 ui.format；本模块只负责界面编排。
"""
import re

import pandas as pd
import streamlit as st

from fundtool import (
    FundApiError,
    format_range,
    get_manager_holding,
    get_manager_holding_history,
)
from fundtool.client import PE_EXTREME
from ui.constants import BENCHMARKS, HISTORY_N, SMALL_NAV_YUAN
from ui.format import (
    _fmt_change,
    _trim_pct,
    fmt_buy_fee,
    fmt_employee_holding_pair,
    fmt_excess,
    fmt_fee_pct,
    fmt_pe,
    pct,
    yi,
)
from ui.query import emp_nums, mgr_change_label
from ui.state import (
    KEY_DETAIL_MANAGER,
    KEY_DETAIL_PAGE,
    KEY_DETAIL_SCOPE,
    KEY_SINGLE_CODE,
    KEY_WITH_HOLDING,
    get_client,
    record_history,
)


def pick_manager_fund(mgr_id, code, name="", page=""):
    """从经理视图里点基金：详情就地显示在该经理视图内（而不是长页面最底部）。
    page 记录详情归属页（"groups"/"single"），避免多页共用会话时详情跨页遗留。"""
    st.session_state[KEY_SINGLE_CODE] = code
    st.session_state[KEY_DETAIL_SCOPE] = "manager"
    st.session_state[KEY_DETAIL_MANAGER] = mgr_id
    st.session_state[KEY_DETAIL_PAGE] = page
    record_history(code, name or (get_client().basic_info(code) or {}).get("SHORTNAME") or code)


def render_fund_detail_section(code):
    """单只基金详情块（成功行 + 展开式详情），经理视图内与单只查询页底部共用"""
    info = get_client().basic_info(code)
    if info is None:
        st.error(f"未找到基金 {code}（代码不存在或已清盘）")
        return
    st.success(f"**{info.get('SHORTNAME')}**（{code}）· {info.get('FTYPE', '')} · {info.get('JJGS', '')}")
    with st.expander("基金详情", expanded=True):
        render_fund_detail(code, with_holding=True)


def _render_fee_block(code):
    """费率块：三项运作费原文（浮费显示各档条款）、申购费首档、赎回费梯度表"""
    st.markdown("**基金费率**")
    try:
        fee = get_client().fee_info(code)
    except FundApiError:
        fee = None
    if fee is None:
        st.caption("费率信息获取失败（网络恢复后重开本页即可）")
    elif not (fee.get("mng_text") or fee.get("cust_text") or fee.get("redeem")):
        st.caption("该基金未披露费率信息")
    else:
        f_left, f_right = st.columns(2)
        f_left.write(
            f"- **管理费率**：{fee.get('mng_text') or '--'}\n"
            f"- **托管费率**：{fee.get('cust_text') or '--'}\n"
            f"- **销售服务费率**：{fee.get('sales_text') or '--'}\n"
            f"- **年费用率合计**：{fmt_fee_pct(fee.get('total'), fee.get('floating'))}%/年"
            + ("（浮动费率，展示为上限档）" if fee.get("floating") else "")
            + "\n"
            f"- **申购费率(首档)**：{fmt_buy_fee(fee)}"
        )
        if fee.get("redeem"):
            f_right.table(pd.DataFrame(fee["redeem"], columns=["持有期限", "赎回费率"]).style.hide(axis="index"))
        else:
            f_right.write("赎回费率：未披露（或无赎回费）")
        st.caption(
            "运作费（管理/托管/销售服务）每日从基金资产计提、净值已扣除，无需另行支付；"
            "申购费为「小于100万元」首档（天天基金优惠价）；赎回费按持有期限递减、先进先出"
        )


def _render_holdings_pe_block(code):
    """重仓股估值块：前十大明细（PE-TTM，亏损红字/微利琥珀字）+ 加权/平均/覆盖度汇总"""
    st.markdown("**重仓股估值（最新季报前十大，PE-TTM）**")
    try:
        hpe = get_client().fund_holdings_pe(code)
    except FundApiError:
        hpe = None
    if hpe is None:
        st.caption("重仓股估值获取失败（网络恢复后重开本页即可）")
    elif not hpe.get("stocks"):
        st.caption("该基金无股票重仓披露（债基/货基或未披露持仓）")
    else:
        rows = []
        for s in hpe["stocks"]:
            pe = s.get("pe")
            if pe is None:
                pe_txt = "--"
            elif pe <= 0:
                pe_txt = "亏损"
            elif pe > PE_EXTREME:  # 微利异常：显示原值（千分位整数+⚠），琥珀字，不参与统计
                pe_txt = f"{pe:,.0f}⚠"
            else:
                pe_txt = f"{pe:.2f}"
            rows.append({
                "股票": s["name"], "代码": s["code"],
                "占净值%": s.get("pct") if s.get("pct") is not None else "--",
                "PE(TTM)": pe_txt,
            })
        hdf = pd.DataFrame(rows)

        def _pe_color(v):
            if v == "亏损":
                return "color: #E53935"
            if v.endswith("⚠"):
                return "color: #B8860B"
            return ""

        hdf = hdf.style.apply(lambda col: [_pe_color(v) for v in col], subset=["PE(TTM)"]).hide(axis="index")
        st.table(hdf)
        cap = (f"前十大合计占净值 {hpe.get('covered_pct')}%；"
               f"加权PE {fmt_pe(hpe.get('weighted'))} · 平均PE {fmt_pe(hpe.get('average'))}")
        if hpe.get("extreme_n"):
            cap += (f" · ⚠ 含 {hpe['extreme_n']} 只微利股(PE>{PE_EXTREME:.0f})，"
                    f"其极端市盈率已计入加权/平均，数字可能被显著放大")
        if hpe.get("loss_n"):
            cap += f" · 亏损/无PE {hpe['loss_n']}只未参与计算"
        st.caption(cap)


def _render_excess_block(code):
    """超额收益对照块：基金各期涨幅 vs 两基准（沪深300 / 偏股混合基金指数930950）。
    基准由中证官网日线计算（与基金阶段涨幅同键口径，直接相减），任一基准获取失败
    该列显示 --，全部失败整块提示。"""
    st.markdown("**超额收益（vs 沪深300 / 偏股混合基金指数）**")
    try:
        ret = get_client().period_returns(code)
    except FundApiError:
        ret = {}
    bms = {}
    try:
        from ui.state import get_macro
        m = get_macro()
        for bk, (_, idx_code) in BENCHMARKS.items():
            try:
                bms[bk] = m.benchmark_returns(idx_code)
            except Exception:
                continue
    except Exception:
        pass
    if not ret:
        st.caption("基金阶段涨幅不可用，超额暂无法计算")
        return
    labels = (("近1月", "r1m"), ("近3月", "r3m"), ("近6月", "r6m"), ("今年以来", "rytd"),
              ("近1年", "r1y"), ("近2年", "r2y"), ("近3年", "r3y"), ("近5年", "r5y"))
    short = {"hs300": "沪深300", "pghh": "偏股混合"}
    rows = []
    for label, key in labels:
        f = ret.get(key)
        r = {"区间": label, "基金": pct(f)}
        for bk, sname in short.items():
            bm = (bms.get(bk) or {}).get(key)
            ex = round(f - bm, 2) if f is not None and bm is not None else None
            r[sname] = pct(bm)
            r[f"vs{sname}"] = fmt_excess(ex)
        rows.append(r)
    st.table(pd.DataFrame(rows).style.apply(_highlight_changes, axis=0).hide(axis="index"))
    st.caption("超额 = 基金 − 基准（百分点，红=跑赢 绿=跑输）；偏股混合基金指数为中证 930950"
               "（Wind 885001 的等价口径），基准序列来自中证官网、缓存 6 小时")


def _render_holding_block(code, info):
    """持有块：经理/从业人员持有（多级别双口径）、报告原文、各级别明细、近几期变化"""
    st.markdown("**基金经理 / 从业人员持有本基金（来自定期报告）**")
    hold = get_manager_holding(get_client(), code)
    if hold["status"] == "ok":
        rng = format_range(hold.get("manager_range"))
        emp_shares, emp_pct = emp_nums(hold.get("employees_exact"))
        emp_cls_val = None
        _m = re.search(r"([A-Z])$", (info.get("SHORTNAME") or "").strip())
        if _m:
            emp_cls_val = (hold.get("employees_exact") or {}).get("classes", {}).get(_m.group(1))
        emp_cls_shares, emp_cls_pct = emp_nums(emp_cls_val)
        m1, m2 = st.columns(2)
        m1.metric("基金经理持有份额（区间）", rng or "见报告原文")
        m2.metric(
            "公司从业人员持有",
            fmt_employee_holding_pair(_m.group(1) if _m else "", emp_cls_pct, emp_shares, emp_pct),
        )
        st.caption(
            f"来源：《{hold.get('report_title')}》（发布于 {hold.get('report_date')}）"
            + (f"  ·  [报告原文 PDF]({hold.get('pdf_url')})" if hold.get("pdf_url") else "")
        )
        if hold.get("manager_line"):
            st.text(f"报告原文：{hold['manager_line']}")
        if hold.get("manager_ranges"):
            parts = "；".join(f"{k} {format_range(v)}" for k, v in hold["manager_ranges"].items())
            st.caption(f"各份额级别明细（展示值为查询代码所属级别）：{parts}")
        emp_classes = (hold.get("employees_exact") or {}).get("classes") or {}
        if emp_classes:
            parts = []
            for k, v in emp_classes.items():
                try:
                    p = _trim_pct(float(str(v.get("pct", "")).replace("%", "")))
                except ValueError:
                    p = "--"
                parts.append(f"{k}类 {v.get('shares')}份({p}%)")
            st.caption(f"从业人员各份额级别（分母为该级别份额，与合计口径不同）：{'；'.join(parts)}")
        try:
            hist = get_manager_holding_history(get_client(), code, HISTORY_N)
        except FundApiError:
            hist = []
        if hist:
            st.markdown("**近几期变化（中报/年报逐期对比）**")
            hdf = pd.DataFrame(
                [
                    {
                        "报告期": f"{h.get('report_date', '')}{'中报' if '中期' in h.get('report_title', '') else '年报'}",
                        "经理持有": format_range(h.get("manager_range")) or "--",
                        "较上期": _fmt_change(h.get("change")),
                    }
                    for h in hist
                ]
            )
            st.table(hdf.style.apply(_highlight_changes, axis=0).hide(axis="index"))
    else:
        st.warning(f"未能获取：{hold.get('error')}")


def render_fund_detail(code, with_holding=None):
    """单只基金的详情（在 expander 内调用）。with_holding=None 时跟随分组查询的勾选项"""
    info = get_client().basic_info(code)
    if info is None:
        st.warning("基本信息不可用")
        return
    if with_holding is None:
        with_holding = st.session_state.get(KEY_WITH_HOLDING)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("单位净值", info.get("DWJZ", "--"), f"{info.get('RZDF', '--')}%（{info.get('FSRQ', '--')}）")
    c2.metric("净资产规模", yi(info.get("ENDNAV")))
    c3.metric("份额规模", yi(info.get("FEGM"), "亿份"))
    c4.metric("风险等级", {"1": "低", "2": "中低", "3": "中", "4": "中高", "5": "高"}.get(str(info.get("RISKLEVEL", "")), "--"))
    try:
        nav = float(info.get("ENDNAV"))
    except (TypeError, ValueError):
        nav = None
    if nav is not None and nav < SMALL_NAV_YUAN:
        st.error("⚠️ 该基金净资产低于 0.5 亿（迷你基金），存在清盘风险，请留意基金公告")
    left, right = st.columns(2)
    left.write(
        f"- **基金公司**：{info.get('JJGS', '--')}\n"
        f"- **成立日期**：{info.get('ESTABDATE', '--')}\n"
        f"- **申购状态**：{info.get('SGZT', '--')} / 赎回：{info.get('SHZT', '--')}\n"
        f"- **重仓股票**：{(info.get('FUNDINVEST') or '--').replace(',', '、')}"
    )
    right.write(
        f"- **基金经理**：{(info.get('JJJL') or '--').replace(',', '、')}\n"
        f"- **今年以来收益**：{info.get('SYL_JN', '--')}%\n"
        f"- **近一年收益**：{info.get('SYL_1N', '--')}%\n"
        f"- **最大回撤(近1年)**：{info.get('MAXRETRA1', '--')}%\n"
        f"- [查看基金档案 ↗](https://fundf10.eastmoney.com/{code}.html)"
    )
    _render_fee_block(code)
    _render_holdings_pe_block(code)
    _render_excess_block(code)
    if with_holding:
        _render_holding_block(code, info)
    tenure = get_client().manager_tenure(code)
    if tenure:
        st.markdown("**基金经理任职情况**")
        _mcl = mgr_change_label(tenure)
        if _mcl:
            st.caption(f"🔁 近一年基金经理有变更：{_mcl}（基金经理单元格已琥珀色提示）")
        tdf = pd.DataFrame(
            [
                {"基金经理": "、".join(t["managers"]), "起始": t["start"], "截止": t["end"],
                 "任职天数": t["days"], "任职回报": f"{t['return_pct']}%"}
                for t in tenure
            ]
        )
        st.table(tdf.style.hide(axis="index"))


# ---------------- 详情小表高亮（Styler，用于本模块 expander 内的小表） ----------------
# 经理持有份额变化：升档绿色 / 降档红色（单元格底色）
_CHG_UP_BG = "background-color: rgba(46,160,67,0.40)"
_CHG_DOWN_BG = "background-color: rgba(229,57,53,0.45)"
# 阶段涨幅（近1月/3月/6月/今年/1年/2年/3年/5年）：正收益红字 / 负收益绿字
# （A 股习惯红涨绿跌；注意与持有较上期的绿=增持红=减持是两套语义）
_RET_UP_FG = "color: #E53935"
_RET_DOWN_FG = "color: #2EA043"


def _highlight_changes(col):
    """涨跌单元格着色（按列，详情 expander 内的 Styler 小表用）：「持有较上期/较上期」↑绿底 ↓红底；
    阶段涨幅列（近1月…近5年）与超额列（vs沪深300/vs偏股混合）正红字 负绿字（A 股红涨绿跌）；
    其他列与 --（未满期/无数据）不着色。总览大表的同类着色在 sortable_table 组件内实现。"""
    name = str(col.name)
    if name in ("持有较上期", "较上期"):
        def bg(v):
            return _CHG_UP_BG if v.startswith("↑") else _CHG_DOWN_BG if v.startswith("↓") else ""
    elif name in ("近1月", "近3月", "近6月", "今年以来", "近1年", "近2年", "近3年", "近5年") or name.startswith("vs"):
        def bg(v):
            if v in ("--", ""):
                return ""
            return _RET_DOWN_FG if v.startswith("-") else _RET_UP_FG
    else:
        return [""] * len(col)
    return [bg(str(v)) for v in col]
