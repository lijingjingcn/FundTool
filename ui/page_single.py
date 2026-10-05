# -*- coding: utf-8 -*-
"""单只/经理查询页：基金代码直达、名称/经理姓名搜索、经理在管基金总览、查询历史"""
import os
import re
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fundtool import FundApiError  # noqa: E402
from ui.common import (  # noqa: E402
    clear_history,
    get_client,
    load_history,
    pick_manager_fund,
    record_history,
    render_fund_detail_section,
    render_manager_funds,
)

st.title("🔍 单只 / 经理查询")
st.caption("输入 6 位基金代码、基金名称关键词或基金经理姓名，按回车或点「🔍 查询」。数据来自天天基金公开数据与基金定期报告，仅供参考。")


# ---------------- 选定基金 / 选定经理 ----------------
def _pick_fund(code, name=""):
    """选定一只基金查看详情（详情显示在页面底部），并记入历史"""
    st.session_state["single_result_code"] = code
    st.session_state["search_matches"] = None
    st.session_state["detail_scope"] = "page"
    record_history(code, name or (get_client().basic_info(code) or {}).get("SHORTNAME") or code)


def _view_manager(m):
    """选定一位基金经理，展示其管理的基金"""
    st.session_state["manager_view"] = m


# 表单内的文本框按「回车」即提交，与点「🔍 查询」按钮等效
with st.form("single_form"):
    _sq, _sgo = st.columns([4, 1])
    _kw = _sq.text_input("基金代码、名称或基金经理", key="single_q",
                         placeholder="基金代码（如 005827）/ 名称关键词（如 蓝筹精选）/ 经理姓名（如 张坤，可一次多位，空格或逗号分隔），回车即查",
                         label_visibility="collapsed")
    _go = _sgo.form_submit_button("🔍 查询", key="single_go", width="stretch")
if _go:
    kw = _kw.strip()
    # 空格/逗号/顿号/分号分隔的多段输入：一次查询多位基金经理，分块展示
    tokens = [t for t in re.split(r"[,，、;；/\s]+", kw) if t]
    if not kw:
        st.warning("请输入基金代码、名称关键词或基金经理姓名")
    elif len(tokens) > 1:
        st.session_state["single_result_code"] = None
        st.session_state["search_matches"] = None
        st.session_state["manager_matches"] = None
        st.session_state["manager_view"] = None
        managers, missing, seen, token_hits = [], [], set(), {}
        for t in tokens:
            if re.search(r"\d", t):  # 经理姓名不含数字；数字段留给基金搜索流程
                missing.append(t)
                continue
            try:
                ms = get_client().search_managers(t)
            except FundApiError:
                ms = []
            token_hits[t] = len(ms)
            new = [m for m in ms if m["id"] not in seen]
            seen.update(m["id"] for m in new)
            if new:
                managers.extend(new)
            else:
                missing.append(t)
        if managers:
            st.session_state["manager_views"] = managers
            st.session_state["manager_missing"] = missing or None
            # 同名经理提示：某个名字匹配到多位不同公司的经理
            st.session_state["manager_dup_names"] = {t: c for t, c in token_hits.items() if c > 1} or None
        else:  # 一个经理都没匹配到：退回按整串做基金名称搜索
            st.session_state["manager_views"] = None
            st.session_state["manager_missing"] = None
            st.session_state["manager_dup_names"] = None
            st.session_state["search_matches"] = get_client().search_funds(kw)
    elif re.fullmatch(r"\d{6}", kw):
        st.session_state["manager_views"] = None
        st.session_state["manager_missing"] = None
        st.session_state["manager_dup_names"] = None
        st.session_state["manager_matches"] = None
        st.session_state["manager_view"] = None
        _pick_fund(kw)
    else:
        st.session_state["single_result_code"] = None
        st.session_state["manager_views"] = None
        st.session_state["manager_missing"] = None
        st.session_state["manager_dup_names"] = None
        st.session_state["search_matches"] = get_client().search_funds(kw)
        # 经理姓名：无官方搜索接口，用全量目录本地匹配（首次拉取约 5~10 秒，之后 7 天内走缓存）
        st.session_state["manager_matches"] = None
        st.session_state["manager_view"] = None
        if len(kw) >= 2 and not re.search(r"\d", kw):
            try:
                st.session_state["manager_matches"] = get_client().search_managers(kw)
            except FundApiError:
                st.session_state["manager_matches"] = []

# ---------------- 基金名称匹配 ----------------
_matches = st.session_state.get("search_matches")
if _matches is not None:
    if not _matches:
        if st.session_state.get("manager_matches"):
            st.info("没有名称匹配的基金，但找到同名基金经理 👇")
        else:
            st.info("没有匹配的基金，换个关键词试试（建议用简称里的两三个字）")
        st.session_state["search_matches"] = None
    else:
        mdict = {m["code"]: m for m in _matches}
        _sel = st.selectbox(
            f"匹配到 {len(_matches)} 只基金，请选择",
            options=list(mdict),
            format_func=lambda c: f"{mdict[c]['name']}（{c}）{mdict[c]['type']}",
            key="single_sel",
        )
        if st.button("查看该基金", key="single_view", type="primary"):
            _pick_fund(_sel, mdict[_sel]["name"])

# ---------------- 经理姓名匹配：经理卡片 + 在管基金总览 ----------------
_mgrs = st.session_state.get("manager_matches")
if _mgrs:
    _mnames = [m["name"] for m in _mgrs]
    _dupn = {n: _mnames.count(n) for n in set(_mnames) if _mnames.count(n) > 1}
    if _dupn:
        st.info("⚠️ 同名基金经理：" + "；".join(
            f"「{n}」{c} 位（不同公司，均已列出，请按所属公司区分）" for n, c in _dupn.items()))
    st.caption(f"👤 匹配到 {len(_mgrs)} 位基金经理（点击查看其管理的基金）")
    for _m in _mgrs:
        st.button(
            f"👤 {_m['name']} · {_m['company']} · 现任 {len(_m['codes'])} 只 · 在管 {_m['scale']}",
            key=f"mgr_{_m['id']}",
            on_click=_view_manager,
            args=(_m,),
            width="stretch",
        )

_mv = st.session_state.get("manager_view")
if _mv:
    st.markdown(f"### 👤 {_mv['name']}（{_mv['company']}）")
    render_manager_funds(_mv)

# 一次输入多位经理：逐个匹配、每位一个分块（可折叠）展示在管基金
_views = st.session_state.get("manager_views")
if _views:
    st.caption(f"👥 共匹配 {len(_views)} 位基金经理，分别展示其在管基金（点开折叠块查看）")
    if st.session_state.get("manager_dup_names"):
        st.info("⚠️ 同名基金经理：" + "；".join(
            f"「{n}」{c} 位（不同公司，均已分块展示，请按所属公司区分）"
            for n, c in st.session_state["manager_dup_names"].items()))
    if st.session_state.get("manager_missing"):
        st.info("未匹配到基金经理：" + "、".join(st.session_state["manager_missing"]))
    for _i, _m in enumerate(_views):
        with st.expander(
            f"👤 {_m['name']} · {_m['company']} · 现任 {len(_m['codes'])} 只 · 在管 {_m['scale']}",
            expanded=_i == 0,
        ):
            render_manager_funds(_m)

# ---------------- 单只基金详情（页面底部；从经理分块点开的详情就地显示在分块内） ----------------
_scode = st.session_state.get("single_result_code")
if _scode and st.session_state.get("detail_scope") != "manager":
    render_fund_detail_section(_scode)

# ---------------- 查询历史 ----------------
_hist = load_history()
if _hist:
    st.divider()
    st.caption("🕘 最近查询（点击再次查看）")
    _hcols = st.columns(4)
    for _i, _h in enumerate(_hist[:12]):
        with _hcols[_i % 4]:
            st.button(f"{(_h.get('name') or _h['code'])[:10]} {_h['code']}", key=f"hist_{_h['code']}",
                      on_click=_pick_fund, args=(_h["code"], _h.get("name") or ""), width="stretch")
    if st.button("清空历史", key="hist_clear"):
        clear_history()
        st.rerun()
