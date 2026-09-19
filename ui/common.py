# -*- coding: utf-8 -*-
"""各页面共享的逻辑：数据客户端、本地持久化（我的基金/查询历史）、
单只基金总览行与详情渲染、总览表高亮、分组结果渲染。"""
import datetime
import json
import os
import re
import time
import uuid

import pandas as pd
import streamlit as st

from fundtool import (
    EastFundClient,
    FundApiError,
    JsonCache,
    format_range,
    get_manager_holding,
    get_manager_holding_history,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试通过 FUNDTOOL_DATA_FILE 指向临时文件，避免冒烟测试覆盖真实分组数据
DATA_FILE = os.environ.get("FUNDTOOL_DATA_FILE") or os.path.join(BASE_DIR, "我的基金.json")
# 单只基金查询的历史记录（FUNDTOOL_HISTORY_FILE 供测试重定向）
HISTORY_FILE = os.environ.get("FUNDTOOL_HISTORY_FILE") or os.path.join(BASE_DIR, "查询历史.json")
HISTORY_MAX = 30  # 最多保留条数
# 查询不设总量上限：全部代码自动分批处理，批次之间稍作停顿以免请求过密。
# 首次查询每只需下载解析报告PDF（约2~5秒/只），已查过的走缓存。
BATCH_SIZE = 30
BATCH_PAUSE = 8  # 批间停顿秒数
# 迷你基金阈值（元）：净资产低于 5000 万的基金有清盘风险，界面特殊提醒
SMALL_NAV_YUAN = 0.5e8
# 经理持有份额对比的报告期数：2 = 当前期报 + 上一份中报/年报
HISTORY_N = 2
# 经理变更提示窗口（天）：近半年内有新任/离任时，基金经理单元格琥珀色提示
MGR_CHANGE_DAYS = 183


def _mgr_change_label(tenure_rows):
    """近半年基金经理变更：'新任' / '离任' / '新任+离任'，无变更返回 ''。
    新任 = 现任经理的上任日期在窗口内；离任 = 有经理的离任日期在窗口内。"""
    threshold = datetime.date.today() - datetime.timedelta(days=MGR_CHANGE_DAYS)
    new = left = False
    for t in tenure_rows or []:
        try:
            start = datetime.date.fromisoformat(str(t.get("start", ""))[:10])
        except ValueError:
            start = None
        try:
            end = datetime.date.fromisoformat(str(t.get("end", ""))[:10])
        except ValueError:
            end = None
        if t.get("end") == "至今" and start and start > threshold:
            new = True
        if t.get("end") != "至今" and end and end > threshold:
            left = True
    return "+".join(p for p, on in (("新任", new), ("离任", left)) if on)


@st.cache_resource
def get_client():
    return EastFundClient(JsonCache(os.path.join(BASE_DIR, ".cache")))


# ---------------- 本地持久化：分组 / 查询历史 ----------------
def load_groups():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            groups = (json.load(f) or {}).get("groups") or []
    except (OSError, ValueError):
        groups = []
    if not groups:
        groups = [{"id": uuid.uuid4().hex[:8], "name": "我的基金", "codes": ""}]
    for g in groups:
        g.setdefault("id", uuid.uuid4().hex[:8])
        g.setdefault("name", "分组")
        g.setdefault("codes", "")
    return groups


def save_groups(groups):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, ensure_ascii=False, indent=2)


def load_history():
    """单只查询历史：[{code,name,ts}]，最新在前"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            items = (json.load(f) or {}).get("items") or []
    except (OSError, ValueError):
        items = []
    return [it for it in items if isinstance(it, dict) and it.get("code")]


def record_history(code, name):
    """记录一次查询：该代码移到最前，去重，截断到 HISTORY_MAX"""
    items = [it for it in load_history() if it.get("code") != code]
    items.insert(0, {"code": code, "name": name or code, "ts": time.strftime("%Y-%m-%d %H:%M")})
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": items[:HISTORY_MAX]}, f, ensure_ascii=False, indent=2)


def clear_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": []}, f, ensure_ascii=False, indent=2)


def new_group(name):
    return {"id": uuid.uuid4().hex[:8], "name": name, "codes": ""}


def parse_codes(raw: str):
    """从输入文本中提取 6 位基金代码，保持顺序去重"""
    codes = re.findall(r"\b\d{6}\b", raw or "")
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def count_codes(raw: str):
    """统计输入文本中各基金代码出现的次数（用于组内重复提示）"""
    codes = re.findall(r"\b\d{6}\b", raw or "")
    counts = {}
    for c in codes:
        counts[c] = counts.get(c, 0) + 1
    return counts


def yi(value, unit="亿"):
    try:
        return f"{float(value) / 1e8:.2f}{unit}"
    except (TypeError, ValueError):
        return "--"


def nav_yuan(value):
    """净资产原始值（元），解析失败返回 None"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_change(chg):
    """'升1档'/'降2档'/'持平' -> '↑1档'/'↓2档'/'→持平'"""
    if not chg:
        return "--"
    if chg == "持平":
        return "→持平"
    return ("↑" if chg.startswith("升") else "↓") + chg[1:]


# ---------------- 单只基金：总览行 / 详情 / 批量查询 ----------------
def fund_overview_row(code, with_holding):
    """单只基金的总览行（分组查询与经理基金列表共用）。
    返回 (row, 是否迷你基金, 持有变化原始值如"升2档", 经理变更标签"新任/离任/新任+离任"或"")；
    基金不存在时 row=None。基本信息网络失败抛 FundApiError；持有份额失败不致命，列内显示失败原因。"""
    info = get_client().basic_info(code)
    if info is None:
        return None, False, "", ""
    nav = nav_yuan(info.get("ENDNAV"))
    scale = yi(info.get("ENDNAV"))
    small = nav is not None and nav < SMALL_NAV_YUAN
    if small:
        scale += " ⚠️"
    row = {"代码": code, "名称": info.get("SHORTNAME", "--"), "类型": info.get("FTYPE", "--"),
           "基金经理": (info.get("JJJL") or "--").replace(",", "、"),
           "规模(净资产)": scale,
           "规模日期": (info.get("FEGMRQ") or "--").replace(" 00:00:00", ""),
           "净值日期": info.get("FSRQ", "--")}
    chg = ""
    if with_holding:
        try:
            hold = get_manager_holding(get_client(), code)
            if hold["status"] == "ok":
                tag = "中报" if "中期" in hold.get("report_title", "") else "年报"
                rng = format_range(hold.get("manager_range"))
                row["基金经理持有本基金"] = rng or "见报告原文"
                row["持有数据来源"] = f"{hold.get('report_date', '')}{tag}"
            else:
                row["基金经理持有本基金"] = "--"
                row["持有数据来源"] = hold.get("error", "获取失败")
            # 与上一份中报/年报对比（各报告解析结果永久缓存，只有新报告需要下载）
            try:
                hist = get_manager_holding_history(get_client(), code, HISTORY_N)
                chg = hist[0].get("change") if hist else ""
            except FundApiError:
                chg = ""
        except FundApiError:
            row["基金经理持有本基金"] = "--"
            row["持有数据来源"] = "获取失败"
    row["持有较上期"] = _fmt_change(chg)
    # 近半年经理变更（任职记录 12 小时缓存，接口失败返回 [] 不致命）
    mgr_chg = _mgr_change_label(get_client().manager_tenure(code))
    return row, small, chg, mgr_chg


def query_all(codes, with_holding, status_box, progress_bar):
    """逐只查询，自动分批直到全部完成。
    返回 {代码: 行数据}、{代码: 错误}、{代码: 规模文案（迷你基金）}、
    {代码: 持有变化（升N档/降N档）}、{代码: 经理变更标签（新任/离任）}"""
    results, errors, small_nav, chg_map, mgr_change = {}, {}, {}, {}, {}
    total = len(codes)
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    for i, code in enumerate(codes):
        if i > 0 and i % BATCH_SIZE == 0:
            status_box.update(label=f"第 {i // BATCH_SIZE}/{n_batches} 批完成，批间停顿 {BATCH_PAUSE} 秒…")
            time.sleep(BATCH_PAUSE)
        try:
            row, small, chg, mgr_chg = fund_overview_row(code, with_holding)
            if row is None:
                errors[code] = "未找到该基金（代码不存在或已清盘）"
                continue
            if small:
                small_nav[code] = row["规模(净资产)"]
            if chg and chg != "持平":
                chg_map[code] = chg
            if mgr_chg:
                mgr_change[code] = mgr_chg
            results[code] = row
        except FundApiError as e:
            errors[code] = str(e)
        status_box.update(label=f"正在查询第 {i + 1}/{total} 只（第 {i // BATCH_SIZE + 1}/{n_batches} 批）")
        progress_bar.progress((i + 1) / total, text=f"查询进度 {i + 1}/{total}（共 {n_batches} 批）")
    return results, errors, small_nav, chg_map, mgr_change


def render_fund_detail(code, with_holding=None):
    """单只基金的详情（在 expander 内调用）。with_holding=None 时跟随分组查询的勾选项"""
    info = get_client().basic_info(code)
    if info is None:
        st.warning("基本信息不可用")
        return
    if with_holding is None:
        with_holding = st.session_state.get("with_holding")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("单位净值", info.get("DWJZ", "--"), f"{info.get('RZDF', '--')}%（{info.get('FSRQ', '--')}）")
    c2.metric("净资产规模", yi(info.get("ENDNAV")))
    c3.metric("份额规模", yi(info.get("FEGM"), "亿份"))
    c4.metric("风险等级", {"1": "低", "2": "中低", "3": "中", "4": "中高", "5": "高"}.get(str(info.get("RISKLEVEL", "")), "--"))
    if nav_yuan(info.get("ENDNAV")) is not None and nav_yuan(info.get("ENDNAV")) < SMALL_NAV_YUAN:
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
        _mcl = _mgr_change_label(tenure)
        if _mcl:
            st.caption(f"🔁 近半年基金经理有变更：{_mcl}（基金经理单元格已琥珀色提示）")
        tdf = pd.DataFrame(
            [
                {"基金经理": "、".join(t["managers"]), "起始": t["start"], "截止": t["end"],
                 "任职天数": t["days"], "任职回报": f"{t['return_pct']}%"}
                for t in tenure
            ]
        )
        st.table(tdf.style.hide(axis="index"))


# ---------------- 总览表高亮 ----------------
# 跨分组重复行的底色（半透明琥珀色，深浅主题下都可读）
_DUP_BG = "background-color: rgba(255,170,0,0.32)"
# 迷你基金（净资产<0.5亿）规模单元格的底色（半透明红色）
_SMALL_BG = "background-color: rgba(229,57,53,0.45)"
# 经理持有份额变化：升档绿色 / 降档红色
_CHG_UP_BG = "background-color: rgba(46,160,67,0.40)"
_CHG_DOWN_BG = "background-color: rgba(229,57,53,0.45)"
# 近半年基金经理变更：基金经理单元格琥珀色
_MGRCHG_BG = "background-color: rgba(255,170,0,0.45)"


def _highlight_mgr_change(changed_codes, codes):
    """基金经理列按行高亮：近半年经理有变更的行，基金经理单元格上底色"""

    def _hl(col):
        if col.name != "基金经理":
            return [""] * len(col)
        return [_MGRCHG_BG if c in changed_codes else "" for c in codes]

    return _hl


def _highlight_dup_rows(dup_codes):
    def _hl(row):
        return [_DUP_BG if row["代码"] in dup_codes else ""] * len(row)

    return _hl


def _highlight_small_scale(small_codes, codes):
    """规模列按行高亮：只给迷你基金所在行的「规模(净资产)」单元格上底色"""

    def _hl(col):
        if col.name != "规模(净资产)":
            return [""] * len(col)
        return [_SMALL_BG if c in small_codes else "" for c in codes]

    return _hl


def _highlight_changes(col):
    """持有变化列（总览「持有较上期」/ 详情「较上期」）单元格着色：↑绿 ↓红"""
    if col.name not in ("持有较上期", "较上期"):
        return [""] * len(col)
    out = []
    for v in col:
        v = str(v)
        out.append(_CHG_UP_BG if v.startswith("↑") else _CHG_DOWN_BG if v.startswith("↓") else "")
    return out


def render_group(name, codes, results):
    """一个分组的结果：总览表 + 每只基金详情。重复的基金整行高亮。"""
    dup_codes = st.session_state.get("dup_codes") or set()
    code_groups = st.session_state.get("code_groups") or {}
    dup_within = (st.session_state.get("dup_within") or {}).get(name, {})
    highlight = set(dup_codes) | set(dup_within)
    rows = [results[c] for c in codes if c in results]
    if rows:
        df = pd.DataFrame(rows)
        small_nav = st.session_state.get("small_nav") or {}
        small_here = {c for c in df["代码"] if c in small_nav}
        mgr_change = st.session_state.get("mgr_change") or {}
        chg_here = {c for c in df["代码"] if c in mgr_change}
        # 用静态 HTML 表格渲染（st.dataframe 是画布渲染，文字无法鼠标划选复制）
        styler = df.style.apply(_highlight_dup_rows(highlight), axis=1)
        if small_here:
            styler = styler.apply(_highlight_small_scale(small_here, df["代码"]), axis=0)
        if chg_here:
            styler = styler.apply(_highlight_mgr_change(chg_here, df["代码"]), axis=0)
        if "持有较上期" in df.columns:
            styler = styler.apply(_highlight_changes, axis=0)
        st.table(styler.hide(axis="index"))
        st.download_button(
            "⬇️ 导出本组 CSV",
            df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"基金信息-{name}.csv",
            mime="text/csv",
            key=f"csv_{name}",
        )
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
