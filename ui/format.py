# -*- coding: utf-8 -*-
"""展示格式化与总览表列定义（纯函数，无 Streamlit 依赖，便于离线测试）。

核心是 OVERVIEW_COLUMNS 列注册表：总览表的每一列（列名/展示格式化/排序键）集中登记，
display_row 与 display_sort_values 由注册表生成。加新列只需两步：
query.fund_overview_row 加原始字段 + 这里登记一行，不再四处同步
（此前加一列要同时改 display_row / display_sort_values / 经理视图兜底行三处）。

原始行的字段契约见 query.fund_overview_row 的 docstring：数值保留原始类型，
排序键、预警判断都基于原始值；RAW_ROW_DEFAULTS 是全字段默认值（兜底行用）。
"""
import math
import re

from pypinyin import lazy_pinyin

from fundtool import format_range, range_rank


# ---------------- 基础格式化 ----------------
def yi(value, unit="亿"):
    try:
        return f"{float(value) / 1e8:.2f}{unit}"
    except (TypeError, ValueError):
        return "--"


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


def _trim_pct(pct_val):
    """占比数值 -> 最多 4 位小数去尾零的字符串（8.335325 -> '8.3353'）"""
    return f"{pct_val:.4f}".rstrip("0").rstrip(".")


def fmt_employee_holding(shares, pct_val):
    """从业人员精确份额 -> '903.84万份(8.3353%)'（份额过万折算万份、占比最多4位小数去尾零；
    0 -> '0（无人持有）'；None -> '--'）。展示格式化集中在此，别处不得拼展示文案。"""
    if shares is None:
        return "--"
    if shares == 0:
        return "0（无人持有）"
    body = f"{shares / 1e4:,.2f}万份" if shares >= 1e4 else f"{shares:,.2f}份"
    if pct_val is None:
        return body
    return f"{body}({_trim_pct(pct_val)}%)"


def fmt_employee_holding_pair(cls_letter, cls_pct, tot_shares, tot_pct):
    """多级别基金的双口径：'A类 6.81% / 合计 225.93万份(3.46%)'——查询代码所属
    级别按该级别份额为分母、合计按全基金总份额为分母；级别行缺失时退回合计
    单值格式（fmt_employee_holding）。"""
    if not cls_letter or cls_pct is None:
        return fmt_employee_holding(tot_shares, tot_pct)
    return f"{cls_letter}类 {_trim_pct(cls_pct)}% / 合计 {fmt_employee_holding(tot_shares, tot_pct)}"


def fmt_fee_pct(value, floating=False):
    """年费用率原始值 -> '1.40'（%/年，浮费型取上限档加 *）；None -> '--'（未披露/获取失败）"""
    if value is None:
        return "--"
    return f"{value:.2f}{'*' if floating else ''}"


def fmt_buy_fee(fee):
    """F10 费率 -> 申购费首档文案：'1.50% → 天天基金 0.15%'（无折扣档只显原价；无表 '--'）"""
    orig = (fee or {}).get("buy_orig") or ""
    if not orig:
        return "--"
    disc = (fee or {}).get("buy_disc") or ""
    return f"{orig} → 天天基金 {disc}" if disc else orig


def fmt_pe(value, loss_n=0):
    """重仓股 PE-TTM 原始值 -> '27.5'（一位小数）；剔除了亏损/无PE 股票时追加后缀
    '剔N'（如 '32.8剔2'，口径透明）；None -> '--'（无重仓股/债基/获取失败）"""
    try:
        return f"{float(value):.1f}" + (f"剔{loss_n}" if loss_n else "")
    except (TypeError, ValueError):
        return "--"


def fmt_excess(value):
    """超额收益原始值（百分点）-> '+5.2' / '-3.1'（带符号一位小数）；None -> '--'"""
    try:
        return f"{float(value):+.1f}"
    except (TypeError, ValueError):
        return "--"


def pinyin_key(name):
    """名称 -> 拼音排序键（小写、音节空格分隔；非汉字片段原样保留）。
    浏览器 localeCompare 的中文排序各内核不一，显式拼音键保证「名称」列
    点表头在任何浏览器下都按拼音排序。多音字取常用读音。"""
    return " ".join(lazy_pinyin(str(name or ""))).lower()


# ---------------- 总览表列注册表 ----------------
def _scale_cell(row):
    """规模列展示：'204.16亿'，迷你基金追加 ⚠️"""
    scale = yi(row.get("nav_yuan"))
    return scale + " ⚠️" if row.get("small") else scale


def _holding_cell(row):
    return format_range(row.get("holding")) or "--"


def _emp_cell(row):
    return fmt_employee_holding_pair(
        row.get("emp_cls"), row.get("emp_cls_pct"), row.get("emp_shares"), row.get("emp_pct")
    )


# (列名, 格式化 row->展示文本, 排序键 row->float|str|None)。None = 按单元格文本排序。
# 列顺序即表格列序：基础属性/费用/估值/持有信息在前，阶段涨幅与同类排名放表尾
# （比较估值时不用左右滚动）。涨跌数值的「正红负绿」与排名的「前25%红/后25%绿」
# 着色在 sortable_table 组件内按列名实现，这里只管文本与排序键。
OVERVIEW_COLUMNS = [
    ("代码", lambda r: r["代码"], lambda r: str(r.get("代码") or "")),
    ("名称", lambda r: r["名称"], lambda r: pinyin_key(r.get("名称"))),
    ("类型", lambda r: r["类型"], None),
    ("基金经理", lambda r: r["基金经理"], None),
    ("规模(净资产)", _scale_cell, lambda r: r.get("nav_yuan")),
    ("年费用率%", lambda r: fmt_fee_pct(r.get("fee_total"), r.get("fee_floating")),
     lambda r: r.get("fee_total")),
    ("持仓加权PE", lambda r: fmt_pe(r.get("pe_w"), r.get("pe_loss_n") or 0),
     lambda r: r.get("pe_w")),
    ("持仓平均PE", lambda r: fmt_pe(r.get("pe_avg"), r.get("pe_loss_n") or 0),
     lambda r: r.get("pe_avg")),
    ("规模日期", lambda r: r["规模日期"], None),
    ("净值日期", lambda r: r["净值日期"], None),
    ("基金经理持有本基金", _holding_cell, lambda r: range_rank(r.get("holding"))),
    ("从业人员持有本基金", _emp_cell, lambda r: r.get("emp_shares")),
    ("持有数据来源", lambda r: r.get("holding_src") or "--", None),
    ("持有较上期", lambda r: _fmt_change(r.get("holding_chg")),
     lambda r: _chg_sort_key(r.get("holding_chg"))),
    ("被FOF持有", lambda r: f"{r['fof_held']}只" if r.get("fof_held") is not None else "--",
     lambda r: r.get("fof_held")),
    ("近1月", lambda r: pct(r.get("r1m")), lambda r: r.get("r1m")),
    ("近3月", lambda r: pct(r.get("r3m")), lambda r: r.get("r3m")),
    ("近6月", lambda r: pct(r.get("r6m")), lambda r: r.get("r6m")),
    ("今年以来", lambda r: pct(r.get("rytd")), lambda r: r.get("rytd")),
    ("近1年", lambda r: pct(r.get("r1y")), lambda r: r.get("r1y")),
    ("近2年", lambda r: pct(r.get("r2y")), lambda r: r.get("r2y")),
    ("近3年", lambda r: pct(r.get("r3y")), lambda r: r.get("r3y")),
    ("近5年", lambda r: pct(r.get("r5y")), lambda r: r.get("r5y")),
    ("同类排名(近1年)", lambda r: peer_pct(r.get("r1y_peer_pct")), lambda r: r.get("r1y_peer_pct")),
    ("同类排名(近3年)", lambda r: peer_pct(r.get("r3y_peer_pct")), lambda r: r.get("r3y_peer_pct")),
    ("同类排名(近5年)", lambda r: peer_pct(r.get("r5y_peer_pct")), lambda r: r.get("r5y_peer_pct")),
]

# 原始行（query.fund_overview_row 契约）的全字段默认值：经理视图在基本信息拿不到时的
# 兜底行由 {**RAW_ROW_DEFAULTS, ...覆盖} 生成，新增字段自动跟随、不再漏改。
RAW_ROW_DEFAULTS = {
    "代码": "", "名称": "--", "类型": "--", "基金经理": "--",
    "规模日期": "--", "净值日期": "--",
    "nav_yuan": None, "small": False,
    "fee_total": None, "fee_floating": False,
    "pe_w": None, "pe_avg": None, "pe_extreme": False, "pe_loss_n": 0,
    "r1m": None, "r3m": None, "r6m": None, "r1y": None, "r2y": None,
    "rytd": None, "r3y": None, "r5y": None,
    "r1y_peer_pct": None, "r3y_peer_pct": None, "r5y_peer_pct": None,
    "holding": "", "emp_shares": None, "emp_pct": None, "emp_cls": "",
    "emp_cls_shares": None, "emp_cls_pct": None,
    "holding_src": "获取失败", "holding_chg": "", "mgr_chg": "",
    "fof_held": None,
}


def display_row(row, bench=None):
    """原始行 -> 总览表展示行（列名即表头，列序即 OVERVIEW_COLUMNS）。纯函数。
    bench 传入基准键（BENCHMARKS 之一）时，表尾追加 3 列超额收益
    （超额-今年以来/近1年/近3年，基准由调用方切换，基金-基准百分点）。"""
    out = {name: fmt(row) for name, fmt, _ in OVERVIEW_COLUMNS}
    ex = (row.get("excess") or {}).get(bench) or {}
    if bench is not None:
        out["超额-今年以来"] = fmt_excess(ex.get("rytd"))
        out["超额-近1年"] = fmt_excess(ex.get("r1y"))
        out["超额-近3年"] = fmt_excess(ex.get("r3y"))
    return out


def display_sort_values(rows, bench=None):
    """原始行列表 -> sortable_table 的排序键（规模按元数、收益/持有按数值、变化按升降档；
    代码按数字、名称按拼音——字符串键由组件按 localeCompare 比较）。纯函数。
    bench 含义同 display_row：追加超额 3 列的数值键。"""
    out = {name: [key(r) for r in rows]
           for name, _, key in OVERVIEW_COLUMNS if key is not None}
    if bench is not None:
        ex = [(r.get("excess") or {}).get(bench) or {} for r in rows]
        out["超额-今年以来"] = [e.get("rytd") for e in ex]
        out["超额-近1年"] = [e.get("r1y") for e in ex]
        out["超额-近3年"] = [e.get("r3y") for e in ex]
    return out
