# -*- coding: utf-8 -*-
"""分组查询结果与经理视图的渲染编排。

单只基金详情在 ui.detail、展示格式化与列定义在 ui.format（列注册表）、
可排序表格组件在 ui.sortable_table；本模块只负责把它们编排成分组结果：
总览表 + 经理汇总/下钻 + 每只基金详情 + 经理视图（在管基金总览与就地详情）。
"""
import re

import pandas as pd
import streamlit as st

from fundtool import FundApiError
from ui.detail import pick_manager_fund, render_fund_detail, render_fund_detail_section
from ui.format import (
    RAW_ROW_DEFAULTS,
    display_row,
    display_sort_values,
    pinyin_key,
)
from ui.query import fund_overview_row, group_manager_agg, manager_dir_index, manager_dir_snapshot
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
)


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
    # 与分组总览同一套列（ui.format 列注册表）：点表头排序（语义化：规模按数值、区间按档位、从业人员按精确份额）
    raw_rows, small_here, mgrchg_here, peext_here = [], set(), set(), set()
    with st.status(f"正在查询 {m['name']} 在管的 {len(m['codes'])} 只基金（含经理/从业人员持有份额，首次查询每只约 3~10 秒）…") as _mst:
        for _i, (_code, _name) in enumerate(zip(m["codes"], m["names"])):
            _mst.update(label=f"正在查询 {_i + 1}/{len(m['codes'])} 只：{_name}")
            try:
                row = fund_overview_row(_code, with_holding=True)
            except FundApiError:
                row = None
            if row is None:  # 基本信息也拿不到：用目录里的名称兜底（默认值清单见 ui.format）
                row = {**RAW_ROW_DEFAULTS, "代码": _code, "名称": _name,
                       "基金经理": m["name"], "holding_src": "获取失败"}
            else:
                if row["small"]:
                    small_here.add(_code)
                if row["mgr_chg"]:
                    mgrchg_here.add(_code)
                if row.get("pe_extreme"):
                    peext_here.add(_code)
            raw_rows.append(row)
        _mst.update(label="查询完成", state="complete", expanded=False)
    st.caption("📊 点击表头排序，再点一次切换升/降序（-- 沉底）；导出的 CSV 与当前显示顺序一致")
    render_sortable_table(
        pd.DataFrame([display_row(r) for r in raw_rows]),
        small_codes=small_here,
        mgr_change_codes=mgrchg_here,
        pe_extreme_codes=peext_here,
        sort_values=display_sort_values(raw_rows),
        title=f"基金信息-经理{m['name']}.csv",
    )
    st.caption("点击基金查看完整详情（含基金经理/从业人员持有份额）")
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


# ---------------- 经理汇总表 ----------------
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
        "基金经理": [pinyin_key(it["mgr"].get("name")) for it in items],
        "公司": [pinyin_key(it["mgr"].get("company")) for it in items],
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
    st.caption("👤 点经理按钮查看其**全部在管基金**（同「单只/经理查询」：含经理/从业人员持有份额、可排序、点基金看详情；首次查询每位经理约需数秒到几十秒）")
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


# ---------------- 分组整体：总览表 + 经理汇总/下钻 + 每只基金详情 ----------------
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
        small_here = {r["代码"] for r in raw_rows if r.get("small")}
        mgrchg_here = {r["代码"] for r in raw_rows if r.get("mgr_chg")}
        peext_here = {r["代码"] for r in raw_rows if r.get("pe_extreme")}
        # 可点表头排序的 HTML 表格（与经理视图/筛选页同款组件）：数值列按原始值排，
        # 「代码」按数字、「名称」按拼音；重复行整行琥珀色。导出 CSV 随当前显示顺序
        st.caption("📊 点击表头排序，再点一次切换升/降序（-- 沉底）；「名称」按拼音、「代码」按代码数字；导出的 CSV 与当前显示顺序一致")
        st.caption("💰 「年费用率%」= 管理+托管+销售服务费（%/年），浮费型取上限档带 *，偏高琥珀底提示；各档原文见基金详情")
        st.caption("📈 「持仓加权PE/平均PE」= 最新季报前十大重仓股 PE-TTM（按占净值比例加权 / 算术平均，亏损股剔除并以「剔N」标注）；仅前十大非全部持仓，明细见基金详情。琥珀底 = 含微利股(PE>500)，数字被极端市盈率放大，比较时需留意")
        render_sortable_table(
            disp,
            small_codes=small_here,
            mgr_change_codes=mgrchg_here,
            dup_codes=highlight,
            pe_extreme_codes=peext_here,
            sort_values=display_sort_values(raw_rows),
            title=f"基金信息-{name}.csv",
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
