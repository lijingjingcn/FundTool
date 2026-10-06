# -*- coding: utf-8 -*-
"""查询与数据整形（不含任何界面渲染）。

核心契约是 fund_overview_row 产出的「原始行」：数值保留原始类型
（nav_yuan 是 float、small 是 bool、holding_chg 是'升N档'原文），
展示格式化（X亿、>100万份、↑2档）由 render.display_row 在渲染时完成——
排序键、预警判断都基于原始值，不再从格式化文本里反向提取数字。
"""
import datetime
import re
import time

import streamlit as st  # 只用 st.session_state：经理目录的会话级快照

from fundtool import FundApiError, get_manager_holding, get_manager_holding_history
from ui.constants import BATCH_PAUSE, BATCH_SIZE, HISTORY_N, MGR_CHANGE_DAYS, SMALL_NAV_YUAN
from ui.state import KEY_MGR_DIR, get_client


# ---------------- 输入解析 ----------------
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


# ---------------- 单只基金：原始行 ----------------
def _nav_value(v):
    """净资产原始值（元），解析失败返回 None"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def mgr_change_label(tenure_rows):
    """近一年基金经理变更：'新任' / '离任' / '新任+离任'，无变更返回 ''。
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


def fund_overview_row(code, with_holding=True):
    """单只基金的原始数据行（分组查询与经理视图共用）。基金不存在返回 None。

    行字段分两类：
    - 直接展示的文本：代码/名称/类型/基金经理/规模日期/净值日期
    - 原始值（渲染层用 render.display_row 格式化）：
      nav_yuan    float|None  期末净资产（元）
      small       bool        迷你基金（净资产 < SMALL_NAV_YUAN，清盘风险）
      r1m/r3m/r6m/r1y/rytd/r3y/r5y float|None  近1月/3月/6月/1年/今年以来/近3年/近5年阶段涨幅（%）
      holding     str         经理持有区间原文（'0'/'10-50'/'>100'…），''=未获取
      holding_src str         持有数据来源（'2026-08-31中报'）或失败原因
      holding_chg str         较上期变化（'升N档'/'降N档'/'持平'），''=无对比
      mgr_chg     str         近一年经理变更（'新任'/'离任'/'新任+离任'），''=无

    基本信息网络失败抛 FundApiError；阶段涨幅/持有份额/任职记录失败不致命，记入对应字段。"""
    info = get_client().basic_info(code)
    if info is None:
        return None
    nav = _nav_value(info.get("ENDNAV"))
    try:
        ret = get_client().period_returns(code)
    except FundApiError:
        ret = {}
    row = {
        "代码": code,
        "名称": info.get("SHORTNAME", "--"),
        "类型": info.get("FTYPE", "--"),
        "基金经理": (info.get("JJJL") or "--").replace(",", "、"),
        "规模日期": (info.get("FEGMRQ") or "--").replace(" 00:00:00", ""),
        "净值日期": info.get("FSRQ", "--"),
        "nav_yuan": nav,
        "small": nav is not None and nav < SMALL_NAV_YUAN,
        "r1m": ret.get("r1m"),
        "r3m": ret.get("r3m"),
        "r6m": ret.get("r6m"),
        "r1y": ret.get("r1y"),
        "rytd": ret.get("rytd"),
        "r3y": ret.get("r3y"),
        "r5y": ret.get("r5y"),
        "holding": "",
        "holding_src": "",
        "holding_chg": "",
        # 近一年经理变更（任职记录 12 小时缓存，接口失败返回 [] 不致命）
        "mgr_chg": mgr_change_label(get_client().manager_tenure(code)),
    }
    if with_holding:
        try:
            hold = get_manager_holding(get_client(), code)
            if hold["status"] == "ok":
                tag = "中报" if "中期" in hold.get("report_title", "") else "年报"
                row["holding"] = hold.get("manager_range") or ""
                row["holding_src"] = f"{hold.get('report_date', '')}{tag}"
            else:
                row["holding_src"] = hold.get("error", "获取失败")
            # 与上一份中报/年报对比（各报告解析结果永久缓存，只有新报告需要下载）
            try:
                hist = get_manager_holding_history(get_client(), code, HISTORY_N)
                row["holding_chg"] = (hist[0].get("change") or "") if hist else ""
            except FundApiError:
                pass
        except FundApiError:
            row["holding_src"] = "获取失败"
    return row


def query_all(codes, with_holding, status_box, progress_bar):
    """逐只查询，自动分批直到全部完成。返回 ({代码: 原始行}, {代码: 错误})。
    迷你基金/增减持/经理变更等预警信息直接读原始行字段（small/holding_chg/mgr_chg），
    不再单独维护派生字典。"""
    results, errors = {}, {}
    total = len(codes)
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    for i, code in enumerate(codes):
        if i > 0 and i % BATCH_SIZE == 0:
            status_box.update(label=f"第 {i // BATCH_SIZE}/{n_batches} 批完成，批间停顿 {BATCH_PAUSE} 秒…")
            time.sleep(BATCH_PAUSE)
        try:
            row = fund_overview_row(code, with_holding)
            if row is None:
                errors[code] = "未找到该基金（代码不存在或已清盘）"
                continue
            results[code] = row
        except FundApiError as e:
            errors[code] = str(e)
        status_box.update(label=f"正在查询第 {i + 1}/{total} 只（第 {i // BATCH_SIZE + 1}/{n_batches} 批）")
        progress_bar.progress((i + 1) / total, text=f"查询进度 {i + 1}/{total}（共 {n_batches} 批）")
    return results, errors


# ---------------- 分组 × 经理目录 聚合 ----------------
def manager_dir_snapshot():
    """经理目录的会话级快照（KEY_MGR_DIR）：多个分组标签页共用一次解析结果；
    获取失败返回 None（不缓存失败，下次调用重试）。"""
    if st.session_state.get(KEY_MGR_DIR) is None:
        try:
            st.session_state[KEY_MGR_DIR] = get_client().manager_dir()
        except (FundApiError, ValueError):
            return None
    return st.session_state[KEY_MGR_DIR]


def group_manager_agg(codes, results, mgr_dir):
    """分组与经理目录的聚合（纯函数，便于测试）。
    返回 [{mgr: 目录行, funds: [(代码, 名称)]}]，按（本组持有只数, 在管规模）降序。
    经理定位优先用「基金代码 ∈ 经理现任代码列表」精确匹配——同名经理天然按人区分；
    该基金在目录里没有记录时（新基金/目录最多 7 天未刷新），按姓名精确匹配兜底。"""
    agg = {}  # 经理ID -> {"mgr": 目录行, "funds": [(代码, 名称)]}
    for code in codes:
        if code not in results:
            continue
        hits = [r for r in mgr_dir if code in (r.get("codes") or [])]
        if not hits:
            names = {n for n in str(results[code].get("基金经理") or "").split("、") if n and n != "--"}
            hits = [r for r in mgr_dir if r.get("name") in names]
        for r in hits:
            funds = agg.setdefault(r["id"], {"mgr": r, "funds": []})["funds"]
            if code not in [c for c, _ in funds]:
                funds.append((code, results[code]["名称"]))
    return sorted(agg.values(), key=lambda it: (-len(it["funds"]), -_dir_scale(it["mgr"]), str(it["mgr"].get("name"))))


def _dir_scale(mgr):
    """经理目录的在管总规模数值（'312.56亿' -> 312.56），仅用于排序"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(mgr.get("scale")))
    return float(m.group()) if m else -1.0
