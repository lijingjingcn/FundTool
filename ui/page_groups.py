# -*- coding: utf-8 -*-
"""分组查询页：侧边栏分组输入（自动保存），主区按分组标签页展示总览与详情"""
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.common import (  # noqa: E402
    BATCH_SIZE,
    count_codes,
    load_groups,
    new_group,
    parse_codes,
    query_all,
    render_group,
    save_groups,
)

if "groups" not in st.session_state:
    st.session_state.groups = load_groups()

st.title("📚 分组查询")
st.caption(
    "按分组输入基金代码，查询：基金名称 · 基金经理 · 基金经理持有本基金份额（定期报告披露）· 基金规模。"
    "输入自动保存在本地（我的基金.json），下次启动直接查询。"
)


# ---------------- 侧边栏：分组输入 ----------------
def _delete_group(gid):
    st.session_state.groups = [g for g in st.session_state.groups if g["id"] != gid]
    save_groups(st.session_state.groups)


def _move_group(gid, delta):
    """上移（delta=-1）/下移（delta=+1）调整分组显示顺序，并持久化"""
    groups = st.session_state.groups
    i = next(idx for idx, g in enumerate(groups) if g["id"] == gid)
    j = i + delta
    if 0 <= j < len(groups):
        groups[i], groups[j] = groups[j], groups[i]
        save_groups(groups)


with st.sidebar:
    st.header("🔎 查询")
    st.caption("输入自动保存到本地（我的基金.json），下次启动直接点“开始查询”")
    for pos, g in enumerate(st.session_state.groups):
        gid = g["id"]
        c_name, c_up, c_down, c_del = st.columns([4, 1, 1, 1])
        c_name.text_input("分组名", value=g["name"], key=f"name_{gid}", label_visibility="collapsed")
        c_up.button("↑", key=f"up_{gid}", on_click=_move_group, args=(gid, -1),
                    disabled=pos == 0, help="上移该分组")
        c_down.button("↓", key=f"down_{gid}", on_click=_move_group, args=(gid, 1),
                      disabled=pos == len(st.session_state.groups) - 1, help="下移该分组")
        c_del.button("🗑", key=f"del_{gid}", on_click=_delete_group, args=(gid,), help="删除该分组")
        st.text_area(
            "基金代码",
            value=g["codes"],
            key=f"codes_{gid}",
            height=100,
            placeholder="基金代码，逗号/空格/换行分隔",
            label_visibility="collapsed",
        )
    if st.button("＋ 添加分组", width="stretch"):
        st.session_state.groups.append(new_group(f"分组{len(st.session_state.groups) + 1}"))
        save_groups(st.session_state.groups)
        st.rerun()
    # 收集编辑后的分组名/代码并保存
    changed = False
    for g in st.session_state.groups:
        gid = g["id"]
        v = st.session_state.get(f"codes_{gid}")
        if v is not None and v != g["codes"]:
            g["codes"] = v
            changed = True
        n = st.session_state.get(f"name_{gid}")
        if n is not None and n.strip() and n.strip() != g["name"]:
            g["name"] = n.strip()
            changed = True
    if changed:
        save_groups(st.session_state.groups)

    with_holding = st.checkbox("查询基金经理持有份额", value=True, help="来自基金中报/年报，首次查询每只基金约需 3~10 秒，之后走本地缓存")
    submitted = st.button("开始查询", type="primary", width="stretch")
    st.divider()
    st.caption(
        "**数据口径说明**\n\n"
        "- 基金规模：最新披露的期末净资产\n"
        "- 经理持有份额：定期报告披露的区间（万份），"
        "一年最多更新两次（中报 8 月底前、年报次年 3 月底前）\n"
        "- 经理变更：近半年有新任/离任时，基金经理单元格琥珀色提示\n\n"
        "查询结果缓存于 `.cache/`，基本信息 12 小时、持有份额 7 天后自动刷新"
    )

# ---------------- 查询 ----------------
if submitted:
    plan = [(g["name"], parse_codes(g["codes"])) for g in st.session_state.groups]
    # 统计跨分组重复：同一代码出现在几个分组
    code_groups = {}
    for gname, g_codes in plan:
        for c in g_codes:
            code_groups.setdefault(c, []).append(gname)
    st.session_state["dup_codes"] = {c for c, gs in code_groups.items() if len(gs) > 1}
    st.session_state["code_groups"] = code_groups
    # 统计组内重复：同一分组里重复输入的代码（查询时仍合并为一条）
    dup_within = {}
    for g in st.session_state.groups:
        repeated = {c: n for c, n in count_codes(g["codes"]).items() if n > 1}
        if repeated:
            dup_within[g["name"]] = repeated
    st.session_state["dup_within"] = dup_within
    all_codes = []
    seen = set()
    for _, codes in plan:
        for c in codes:
            if c not in seen:
                seen.add(c)
                all_codes.append(c)
    if not all_codes:
        st.warning("请先在左侧分组中输入至少一个 6 位基金代码")
    else:
        with st.status(f"正在查询 {len(all_codes)} 只基金（自动分批，每批 {BATCH_SIZE} 只）…", expanded=True) as status_box:
            progress_bar = st.progress(0.0, text="准备查询…")
            results, errors, small_nav, chg_map, mgr_change = query_all(all_codes, with_holding, status_box, progress_bar)
            progress_bar.empty()
            status_box.update(label="查询完成", state="complete", expanded=False)
        st.session_state["results"] = results
        st.session_state["errors"] = errors
        st.session_state["small_nav"] = small_nav
        st.session_state["holding_chg"] = chg_map
        st.session_state["mgr_change"] = mgr_change
        st.session_state["plan"] = plan
        st.session_state["with_holding"] = with_holding

# ---------------- 结果展示：分组标签页 ----------------
results = st.session_state.get("results")
if results is not None:
    dup_codes = st.session_state.get("dup_codes") or set()
    dup_within = st.session_state.get("dup_within") or {}
    if dup_codes:
        dup_desc = "、".join(
            f"`{c}`（{results[c]['名称'] if c in results else ''}）" for c in sorted(dup_codes)
        )
        st.warning(f"🔁 以下基金在多个分组中重复出现（表格中以琥珀色高亮显示）：{dup_desc}")
    if dup_within:
        desc = "；".join(
            f"{g}：" + "、".join(f"`{c}`×{n}" for c, n in codes.items())
            for g, codes in dup_within.items()
        )
        st.warning(f"✍️ 以下分组内重复输入的代码已自动合并为一条（同样高亮显示）：{desc}")
    small_nav = st.session_state.get("small_nav") or {}
    if small_nav:
        small_desc = "、".join(
            f"{results[c]['名称']}（`{c}`，{(results[c].get('规模(净资产)') or '').strip()}）"
            for c in small_nav if c in results
        )
        if small_desc:
            st.error(f"⚠️ 以下 {len(small_nav)} 只基金净资产低于 0.5 亿（迷你基金，规模单元格红色高亮，注意清盘风险）：{small_desc}")
    chg_map = st.session_state.get("holding_chg") or {}
    if chg_map:
        downs = {c: v for c, v in chg_map.items() if v.startswith("降") and c in results}
        ups = {c: v for c, v in chg_map.items() if v.startswith("升") and c in results}
        if downs:
            desc = "、".join(f"{results[c]['名称']}（`{c}`，↓{v[1:]}）" for c, v in downs.items())
            st.warning(f"📉 以下 {len(downs)} 只基金的经理**减持**了本基金（较上一份中报/年报，单元格红色高亮）：{desc}")
        if ups:
            desc = "、".join(f"{results[c]['名称']}（`{c}`，↑{v[1:]}）" for c, v in ups.items())
            st.success(f"📈 以下 {len(ups)} 只基金的经理**增持**了本基金（较上一份中报/年报，单元格绿色高亮）：{desc}")
    mgr_change = st.session_state.get("mgr_change") or {}
    if mgr_change:
        hit = {c: v for c, v in mgr_change.items() if c in results}
        if hit:
            desc = "、".join(f"{results[c]['名称']}（`{c}`，{v}）" for c, v in hit.items())
            st.warning(f"🔁 以下 {len(hit)} 只基金**近半年基金经理有变更**（基金经理单元格琥珀色高亮）：{desc}")
    plan = [(n, [c for c in cs if c not in (st.session_state.get("errors") or {})]) for n, cs in st.session_state.get("plan") or []]
    non_empty = [(n, cs) for n, cs in plan if cs]
    if len(non_empty) > 1:
        tabs = st.tabs([f"{n}（{len(cs)}只）" for n, cs in non_empty])
        for tab, (name, codes) in zip(tabs, non_empty):
            with tab:
                render_group(name, codes, results)
    elif len(non_empty) == 1:
        render_group(non_empty[0][0], non_empty[0][1], results)
    else:
        st.error("所有代码都查询失败，请检查输入")
    errors = st.session_state.get("errors") or {}
    if errors:
        st.subheader("⚠️ 未成功的代码")
        for code, msg in errors.items():
            st.error(f"`{code}`：{msg}")
else:
    st.info(
        "👈 在左侧分组中输入基金代码，点击“开始查询”。示例：005827（易方达蓝筹精选混合）、"
        "110022（易方达消费行业）。可用“＋添加分组”建立自己的分组，结果按分组分标签页展示。"
    )
