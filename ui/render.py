# -*- coding: utf-8 -*-
"""渲染层：原始行数据 -> 界面元素（总览表、详情、经理视图、高亮）。

展示格式化集中在此：display_row 把 query.fund_overview_row 的原始行转成
总览表行（'204.16亿 ⚠️'、'>100万份'、'↑2档'），sortable_table 的数值排序键
（sort_values）也在这里从原始字段算出——组件本身不再从展示文本里反向提取数字。
"""
import math
import re

import pandas as pd
import streamlit as st

from fundtool import (
    FundApiError,
    format_range,
    get_manager_holding,
    get_manager_holding_history,
    range_rank,
)
from ui.constants import HISTORY_N, SMALL_NAV_YUAN, TWO_LINE_HEADERS
from ui.query import (
    fund_overview_row,
    group_manager_agg,
    manager_dir_index,
    manager_dir_snapshot,
    mgr_change_label,
)
from ui.sortable_table import render_sortable_table
from ui.state import (
    KEY_CODE_GROUPS,
    KEY_DETAIL_MANAGER,
    KEY_DETAIL_PAGE,
    KEY_DETAIL_SCOPE,
    KEY_DUP_CODES,
    KEY_DUP_WITHIN,
    KEY_GROUP_MGR_VIEW,
    KEY_SINGLE_CODE,
    KEY_WITH_HOLDING,
    get_client,
    record_history,
)


# ---------------- 展示格式化（唯一的文案拼装点） ----------------
def yi(value, unit="亿"):
    try:
        return f"{float(value) / 1e8:.2f}{unit}"
    except (TypeError, ValueError):
        return "--"


def _fmt_change(chg):
    """'升1档'/'降2档'/'持平' -> '↑1档'/'↓2档'/'→持平'"""
    if not chg:
        return "--"
    if chg == "持平":
        return "→持平"
    return ("↑" if chg.startswith("升") else "↓") + chg[1:]


def _chg_sort_key(chg):
    """'升2档'->2 / '降2档'->-2 / '持平'->0，无法识别返回 None（排序沉底）"""
    m = re.search(r"\d+", str(chg or ""))
    if str(chg).startswith("升"):
        return int(m.group()) if m else None
    if str(chg).startswith("降"):
        return -int(m.group()) if m else None
    return 0 if chg == "持平" else None


def pct(value):
    """阶段涨幅原始值 -> '-2.31%'（None/无效 -> '--'，新基金未满该期）"""
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "--"


def peer_pct(value):
    """同类排名百分位原始值 -> '前96%'（向上取整、最小 1；None -> '--'，新基金未满 1 年）"""
    try:
        return f"前{max(1, math.ceil(float(value)))}%"
    except (TypeError, ValueError):
        return "--"


def display_row(row):
    """原始行 -> 总览表展示行（列名即表头）。格式化集中在此，别处不得拼展示文案。"""
    scale = yi(row.get("nav_yuan"))
    if row.get("small"):
        scale += " ⚠️"
    return {
        "代码": row["代码"],
        "名称": row["名称"],
        "类型": row["类型"],
        "基金经理": row["基金经理"],
        "规模(净资产)": scale,
        "规模日期": row["规模日期"],
        "净值日期": row["净值日期"],
        "近1月": pct(row.get("r1m")),
        "近3月": pct(row.get("r3m")),
        "近6月": pct(row.get("r6m")),
        "今年以来": pct(row.get("rytd")),
        "近1年": pct(row.get("r1y")),
        "近2年": pct(row.get("r2y")),
        "近3年": pct(row.get("r3y")),
        "近5年": pct(row.get("r5y")),
        "同类排名(近1年)": peer_pct(row.get("r1y_peer_pct")),
        "同类排名(近3年)": peer_pct(row.get("r3y_peer_pct")),
        "同类排名(近5年)": peer_pct(row.get("r5y_peer_pct")),
        "基金经理持有本基金": format_range(row.get("holding")) or "--",
        "持有数据来源": row.get("holding_src") or "--",
        "持有较上期": _fmt_change(row.get("holding_chg")),
    }


def display_sort_values(rows):
    """原始行列表 -> sortable_table 的数值排序键（规模按元数、收益/持有按数值、变化按升降档）"""
    return {
        "规模(净资产)": [r.get("nav_yuan") for r in rows],
        "近1月": [r.get("r1m") for r in rows],
        "近3月": [r.get("r3m") for r in rows],
        "近6月": [r.get("r6m") for r in rows],
        "今年以来": [r.get("rytd") for r in rows],
        "近1年": [r.get("r1y") for r in rows],
        "近2年": [r.get("r2y") for r in rows],
        "近3年": [r.get("r3y") for r in rows],
        "近5年": [r.get("r5y") for r in rows],
        "同类排名(近1年)": [r.get("r1y_peer_pct") for r in rows],
        "同类排名(近3年)": [r.get("r3y_peer_pct") for r in rows],
        "同类排名(近5年)": [r.get("r5y_peer_pct") for r in rows],
        "基金经理持有本基金": [range_rank(r.get("holding")) for r in rows],
        "持有较上期": [_chg_sort_key(r.get("holding_chg")) for r in rows],
    }


# ---------------- 单只基金详情 ----------------
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
    if with_holding:
        st.markdown("**基金经理持有本基金（来自定期报告）**")
        hold = get_manager_holding(get_client(), code)
        if hold["status"] == "ok":
            rng = format_range(hold.get("manager_range"))
            m1, m2 = st.columns(2)
            m1.metric("基金经理持有份额（区间）", rng or "见报告原文")
            emp = hold.get("employees_exact") or {}
            m2.metric(
                "公司从业人员合计持有",
                f"{emp.get('shares', '--')} 份" if emp.get("shares") else "--",
                emp.get("pct") or None,
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


# ---------------- 经理视图（单只/经理查询页与分组查询的经理下钻共用） ----------------
def render_manager_funds(m, page=""):
    """一位经理的在管基金总览（单只/经理查询页与分组查询的经理下钻共用）。
    page（"groups"/"single"）用于就地详情的页面归属校验，其他页遗留的详情状态不渲染。
    基金按钮的 key 带经理 ID 前缀：共管基金会在多位经理块中重复出现，避免 key 冲突。"""
    _days = str(m.get("days", "--"))
    _years = f"（约 {int(_days) // 365} 年）" if _days.isdigit() else ""
    st.caption(
        f"现任基金 {len(m['codes'])} 只 · 在管总规模 {m['scale']} · "
        f"累计从业 {_days} 天{_years} · 现任基金最佳回报 {m['best_return']}"
    )
    # 与分组总览同一套列：含基金经理持有本基金、持有较上期；点表头排序（语义化：规模按数值、区间按档位）
    raw_rows, small_here, mgrchg_here = [], set(), set()
    with st.status(f"正在查询 {m['name']} 在管的 {len(m['codes'])} 只基金（含经理持有份额，首次查询每只约 3~10 秒）…") as _mst:
        for _i, (_code, _name) in enumerate(zip(m["codes"], m["names"])):
            _mst.update(label=f"正在查询 {_i + 1}/{len(m['codes'])} 只：{_name}")
            try:
                row = fund_overview_row(_code, with_holding=True)
            except FundApiError:
                row = None
            if row is None:  # 基本信息也拿不到：用目录里的名称兜底
                row = {"代码": _code, "名称": _name, "类型": "--", "基金经理": m["name"],
                       "规模日期": "--", "净值日期": "--", "nav_yuan": None, "small": False,
                       "r1m": None, "r3m": None, "r6m": None, "r1y": None,
                       "r2y": None, "rytd": None, "r3y": None, "r5y": None,
                       "r1y_peer_pct": None, "r3y_peer_pct": None, "r5y_peer_pct": None,
                       "holding": "", "holding_src": "获取失败", "holding_chg": "", "mgr_chg": ""}
            else:
                if row["small"]:
                    small_here.add(_code)
                if row["mgr_chg"]:
                    mgrchg_here.add(_code)
            raw_rows.append(row)
        _mst.update(label="查询完成", state="complete", expanded=False)
    st.caption("📊 点击表头排序，再点一次切换升/降序（-- 沉底）；导出的 CSV 与当前显示顺序一致")
    render_sortable_table(
        pd.DataFrame([display_row(r) for r in raw_rows]),
        small_codes=small_here,
        mgr_change_codes=mgrchg_here,
        sort_values=display_sort_values(raw_rows),
        title=f"基金信息-经理{m['name']}.csv",
    )
    st.caption("点击基金查看完整详情（含基金经理持有份额）")
    _mvcols = st.columns(4)
    for _i, (_code, _name) in enumerate(zip(m["codes"], m["names"])):
        with _mvcols[_i % 4]:
            st.button(f"{_name[:10]} {_code}", key=f"mvfund_{m['id']}_{_code}",
                      on_click=pick_manager_fund, args=(m["id"], _code, _name, page), width="stretch")
    # 就地详情：点本视图的基金，详情显示在本视图内（共管基金只显示在被点击的经理视图）；
    # 页面归属须一致——分组页/单只页共用会话，否则另一页的同经理视图会渲染出遗留详情
    if (st.session_state.get(KEY_DETAIL_SCOPE) == "manager"
            and st.session_state.get(KEY_DETAIL_PAGE) == page
            and st.session_state.get(KEY_DETAIL_MANAGER) == m["id"]
            and st.session_state.get(KEY_SINGLE_CODE)):
        render_fund_detail_section(st.session_state[KEY_SINGLE_CODE])


# 「组名(子项)」列名（如 同类排名(近1年)）；连续≥2列同组才合并，单独的「规模(净资产)」不算
_GROUPED_COL = re.compile(r"^(?P<grp>[^(]+)\((?P<sub>[^)]+)\)$")


def grouped_header_df(df):
    """平铺列名的展示表 -> 两行表头（MultiIndex）：相邻≥2列同名「组(子项)」时，
    第一行为组名（跨列合并）、第二行为子项；其余列第一行为空。
    TWO_LINE_HEADERS 里的超长列名也拆成上行/下行两行。CSV 导出仍用平铺 df。"""
    cols = list(df.columns)
    tops = [""] * len(cols)
    i = 0
    while i < len(cols):
        m = _GROUPED_COL.match(str(cols[i]))
        if m:
            j = i
            while j < len(cols) and (mj := _GROUPED_COL.match(str(cols[j]))) and mj.group("grp") == m.group("grp"):
                j += 1
            if j - i >= 2:
                for k in range(i, j):
                    tops[k] = m.group("grp")
                i = j
                continue
        i += 1
    subs = []
    for c, t in zip(cols, tops):
        if t:
            subs.append(_GROUPED_COL.match(str(c)).group("sub"))
        elif str(c) in TWO_LINE_HEADERS:
            subs.append(TWO_LINE_HEADERS[str(c)][1])
        else:
            subs.append(str(c))
    tops = [TWO_LINE_HEADERS[str(c)][0] if not t and str(c) in TWO_LINE_HEADERS else t
            for c, t in zip(cols, tops)]
    out = df.copy()
    out.columns = pd.MultiIndex.from_arrays([tops, subs])
    return out


def _styler_col_name(col):
    """Styler 列名归一：MultiIndex ('同类排名','近1年') -> '同类排名(近1年)'、
    ('','代码') -> '代码'；普通单级列名原样返回。让高亮函数对两套表头通用。"""
    n = col.name
    if isinstance(n, tuple) and len(n) == 2:
        for flat, (a, b) in TWO_LINE_HEADERS.items():
            if n == (a, b):
                return flat
        top, sub = (str(x) for x in n)
        return f"{top}({sub})" if top else sub
    return str(n)


# ---------------- 总览表高亮 ----------------
# 跨分组重复行的底色（半透明琥珀色，深浅主题下都可读）
_DUP_BG = "background-color: rgba(255,170,0,0.32)"
# 迷你基金（净资产<0.5亿）规模单元格的底色（半透明红色）
_SMALL_BG = "background-color: rgba(229,57,53,0.45)"
# 经理持有份额变化：升档绿色 / 降档红色（单元格底色）
_CHG_UP_BG = "background-color: rgba(46,160,67,0.40)"
_CHG_DOWN_BG = "background-color: rgba(229,57,53,0.45)"
# 阶段涨幅（近1月/3月/6月/今年/1年/2年/3年/5年）：正收益红字 / 负收益绿字
# （A 股习惯红涨绿跌；注意与持有较上期的绿=增持红=减持是两套语义）
_RET_UP_FG = "color: #E53935"
_RET_DOWN_FG = "color: #2EA043"
# 近一年基金经理变更：基金经理单元格琥珀色
_MGRCHG_BG = "background-color: rgba(255,170,0,0.45)"


def _highlight_mgr_change(changed_codes, codes):
    """基金经理列按行高亮：近一年经理有变更的行，基金经理单元格上底色"""

    def _hl(col):
        if _styler_col_name(col) != "基金经理":
            return [""] * len(col)
        return [_MGRCHG_BG if c in changed_codes else "" for c in codes]

    return _hl


def _highlight_dup_rows(dup_codes):
    def _hl(row):
        # 代码 恒为展示表第一列；MultiIndex 表头下行标签是 ('','代码') 元组，按位置取更稳
        return [_DUP_BG if row.iloc[0] in dup_codes else ""] * len(row)

    return _hl


def _highlight_small_scale(small_codes, codes):
    """规模列按行高亮：只给迷你基金所在行的「规模(净资产)」单元格上底色"""

    def _hl(col):
        if _styler_col_name(col) != "规模(净资产)":
            return [""] * len(col)
        return [_SMALL_BG if c in small_codes else "" for c in codes]

    return _hl


def _highlight_changes(col):
    """涨跌单元格着色（按列）：「持有较上期/较上期」↑绿底 ↓红底；
    阶段涨幅列（近1月/近3月/近6月/今年以来/近1年/近2年/近3年/近5年）正红字 负绿字（A 股红涨绿跌）；
    同类排名(近X年) 各期 前25%红字、后25%绿字（小=好，红=好）；
    其他列与 --（未满期/无数据）不着色"""
    name = _styler_col_name(col)
    if name in ("持有较上期", "较上期"):
        def bg(v):
            return _CHG_UP_BG if v.startswith("↑") else _CHG_DOWN_BG if v.startswith("↓") else ""
    elif name.startswith("同类排名("):
        def bg(v):
            m = re.fullmatch(r"前(\d+)%", str(v))
            if not m:
                return ""
            p = int(m.group(1))
            return _RET_UP_FG if p <= 25 else _RET_DOWN_FG if p > 75 else ""
    elif name in ("近1月", "近3月", "近6月", "今年以来", "近1年", "近2年", "近3年", "近5年"):
        def bg(v):
            if v in ("--", ""):
                return ""
            return _RET_DOWN_FG if v.startswith("-") else _RET_UP_FG
    else:
        return [""] * len(col)
    return [bg(str(v)) for v in col]


# ---------------- 分组结果：经理汇总 + 经理下钻 + 分组整体 ----------------
def manager_summary_rows(items):
    """经理聚合项 -> 汇总表展示行（纯函数，便于测试），列含义见 render_manager_summary。"""
    rows = []
    for it in items:
        r = it["mgr"]
        days = str(r.get("days", "--"))
        years = f"（约{int(days) // 365}年）" if days.isdigit() else ""
        rows.append({
            "基金经理": r.get("name") or "--",
            "公司": r.get("company") or "--",
            "本组持有": f"{len(it['funds'])}只",
            "本组基金": "、".join(n for _, n in it["funds"]),
            "现任基金": f"{len(r.get('codes') or [])}只",
            "在管总规模": r.get("scale") or "--",
            "从业天数": f"{days}天{years}" if days.isdigit() else days,
            "现任最佳回报": r.get("best_return") or "--",
        })
    return rows


def _summary_sort_values(items):
    """汇总表的数值排序键（从目录行的原始数据算出）"""
    def _num(v):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None

    return {
        "本组持有": [float(len(it["funds"])) for it in items],
        "现任基金": [float(len(it["mgr"].get("codes") or [])) for it in items],
        "在管总规模": [_num(it["mgr"].get("scale")) for it in items],
        "从业天数": [_num(it["mgr"].get("days")) for it in items],
        "现任最佳回报": [_num(it["mgr"].get("best_return")) for it in items],
    }


def render_manager_summary(name, items):
    """一个分组的经理汇总表：本组每位基金经理的公司、在管基金数、在管总规模、从业年限。
    数据全部来自经理目录快照（本地缓存 7 天），不触发逐基金查询。"""
    if not items:
        return
    with st.expander(f"👥 本组经理汇总（{len(items)} 位）"):
        st.caption(
            "「本组持有」= 该经理在本分组管理的基金数；在管信息来自经理目录快照"
            "（本地缓存 7 天，可能与最新任职有短暂出入）。同名经理按「现任代码精确匹配」区分。"
            "点表头排序（再点切换升降序），右上角可导出 CSV；下方按钮可下钻各经理的全部在管基金"
        )
        render_sortable_table(
            pd.DataFrame(manager_summary_rows(items)),
            sort_values=_summary_sort_values(items),
            title=f"经理汇总-{name}.csv",
        )


def _view_group_manager(group, mgr):
    """分组经理下钻：记录所选分组与经理，视图渲染在该分组内"""
    st.session_state[KEY_GROUP_MGR_VIEW] = {"group": group, "mgr": mgr}


def render_manager_drilldown(name, items):
    """分组结果下方的经理下钻：每位经理一个按钮，点击就地查看其全部在管基金
    （与「单只/经理查询」同款视图：逐基金查询含经理持有份额、点表头排序、点基金看详情）"""
    if not items:
        return
    st.caption("👤 点经理按钮查看其**全部在管基金**（同「单只/经理查询」：含经理持有份额、可排序、点基金看详情；首次查询每位经理约需数秒到几十秒）")
    cols = st.columns(4)
    view = st.session_state.get(KEY_GROUP_MGR_VIEW) or {}
    for i, it in enumerate(items):
        r = it["mgr"]
        with cols[i % 4]:
            st.button(
                f"👤 {r['name']}·{str(r.get('company') or '').replace('基金', '')}",
                key=f"gmgr_{name}_{r['id']}",
                on_click=_view_group_manager,
                args=(name, r),
                help=f"{r.get('company')} · 现任 {len(r.get('codes') or [])} 只 · 在管 {r.get('scale')}",
                width="stretch",
            )
    if view.get("group") == name and view.get("mgr"):
        m = view["mgr"]
        st.markdown(f"##### 👤 {m['name']}（{m.get('company')}）的全部在管基金")
        render_manager_funds(m, page="groups")


def render_group(name, codes, results):
    """一个分组的结果：总览表 + 经理汇总/下钻 + 每只基金详情。重复的基金整行高亮。

    经理目录聚合只算一次，汇总表与下钻按钮共用；目录索引会话级缓存，多组 rerun 不重建。"""
    dup_codes = st.session_state.get(KEY_DUP_CODES) or set()
    code_groups = st.session_state.get(KEY_CODE_GROUPS) or {}
    dup_within = (st.session_state.get(KEY_DUP_WITHIN) or {}).get(name, {})
    highlight = set(dup_codes) | set(dup_within)
    raw_rows = [results[c] for c in codes if c in results]
    if raw_rows:
        disp = pd.DataFrame([display_row(r) for r in raw_rows])
        codes_ordered = [r["代码"] for r in raw_rows]
        small_here = {r["代码"] for r in raw_rows if r.get("small")}
        mgrchg_here = {r["代码"] for r in raw_rows if r.get("mgr_chg")}
        # 用静态 HTML 表格渲染（st.dataframe 是画布渲染，文字无法鼠标划选复制）。
        # 表头两行分组（同类排名 跨 近1年/近3年）：展示用 MultiIndex df，CSV 导出仍用平铺列名
        styler = grouped_header_df(disp).style.apply(_highlight_dup_rows(highlight), axis=1)
        if small_here:
            styler = styler.apply(_highlight_small_scale(small_here, codes_ordered), axis=0)
        if mgrchg_here:
            styler = styler.apply(_highlight_mgr_change(mgrchg_here, codes_ordered), axis=0)
        styler = styler.apply(_highlight_changes, axis=0)
        st.table(styler.hide(axis="index"))
        st.download_button(
            "⬇️ 导出本组 CSV",
            disp.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"基金信息-{name}.csv",
            mime="text/csv",
            key=f"csv_{name}",
        )
        mgr_dir = manager_dir_snapshot()
        if mgr_dir is None:
            st.warning("👥 基金经理目录获取失败，本组经理汇总暂不可用（网络恢复后重开本页即可）")
        else:
            items = group_manager_agg(codes, results, mgr_dir, index=manager_dir_index())
            render_manager_summary(name, items)
            render_manager_drilldown(name, items)
    else:
        st.caption("该分组没有查询成功的基金")
    for code in codes:
        if code in results:
            mark = " 🔁" if code in highlight else ""
            with st.expander(f"{results[code]['名称']}（{code}）{mark}"):
                if code in dup_within:
                    st.caption(f"🔁 该代码在本分组中重复输入了 {dup_within[code]} 次，查询时已合并为一条")
                if code in dup_codes:
                    groups_in = "、".join(code_groups.get(code, []))
                    st.caption(f"🔁 该基金在多个分组中重复出现：{groups_in}")
                render_fund_detail(code)
