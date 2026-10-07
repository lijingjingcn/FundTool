# -*- coding: utf-8 -*-
"""分组查询页：侧边栏分组输入（自动保存），主区按分组标签页展示总览与详情"""
import os
import sys

import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.constants import BATCH_SIZE  # noqa: E402
from ui.query import count_codes, parse_codes, query_all  # noqa: E402
from ui.render import display_row, render_group  # noqa: E402
from ui.state import (  # noqa: E402
    KEY_CODE_GROUPS,
    KEY_DUP_CODES,
    KEY_DUP_WITHIN,
    KEY_ERRORS,
    KEY_GROUPS,
    KEY_GROUP_TABS,
    KEY_PLAN,
    KEY_QUERY_PENDING,
    KEY_RESULTS,
    KEY_WITH_HOLDING,
    load_groups,
    new_group,
    save_groups,
)

if KEY_GROUPS not in st.session_state:
    st.session_state[KEY_GROUPS] = load_groups()

# 点击帧的自动回点脚本：同源 iframe 里延时找侧边栏「开始查询」按钮并程序化点击一次，
# 把第一帧的 armed 状态转成一次真实点击 rerun。ES5 写法；点击成功即停（fired 标记），
# 找不到按钮每 0.5 秒重试；用户抢先手点按钮同样进入第二帧，随时可接管
_KICK_QUERY_HTML = """
<script>
  var fired = false;
  function kick() {
    if (fired) { return; }
    try {
      var btns = window.parent.document.querySelectorAll('button');
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].textContent.indexOf('开始查询') >= 0) {
          fired = true;
          btns[i].click();
          return;
        }
      }
    } catch (err) { /* 跨域拿不到 parent 时靠重试或用户手点兜底 */ }
    setTimeout(kick, 500);
  }
  setTimeout(kick, 400);
</script>
"""

st.title("📚 分组查询")
st.caption(
    "按分组输入基金代码，查询：基金名称 · 基金经理 · 基金经理持有本基金份额（定期报告披露）· 基金规模。"
    "输入自动保存在本地（我的基金.json），下次启动直接查询。"
)


# ---------------- 侧边栏：分组输入 ----------------
def _delete_group(gid):
    st.session_state[KEY_GROUPS] = [g for g in st.session_state[KEY_GROUPS] if g["id"] != gid]
    save_groups(st.session_state[KEY_GROUPS])


def _move_group(gid, delta):
    """上移（delta=-1）/下移（delta=+1）调整分组显示顺序，并持久化"""
    groups = st.session_state[KEY_GROUPS]
    i = next(idx for idx, g in enumerate(groups) if g["id"] == gid)
    j = i + delta
    if 0 <= j < len(groups):
        groups[i], groups[j] = groups[j], groups[i]
        save_groups(groups)


with st.sidebar:
    st.header("🔎 查询")
    st.caption("输入自动保存到本地（我的基金.json），下次启动直接点“开始查询”")
    for pos, g in enumerate(st.session_state[KEY_GROUPS]):
        gid = g["id"]
        c_name, c_up, c_down, c_del = st.columns([4, 1, 1, 1])
        c_name.text_input("分组名", value=g["name"], key=f"name_{gid}", label_visibility="collapsed")
        c_up.button("↑", key=f"up_{gid}", on_click=_move_group, args=(gid, -1),
                    disabled=pos == 0, help="上移该分组")
        c_down.button("↓", key=f"down_{gid}", on_click=_move_group, args=(gid, 1),
                      disabled=pos == len(st.session_state[KEY_GROUPS]) - 1, help="下移该分组")
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
        st.session_state[KEY_GROUPS].append(new_group(f"分组{len(st.session_state[KEY_GROUPS]) + 1}"))
        save_groups(st.session_state[KEY_GROUPS])
        st.rerun()
    # 收集编辑后的分组名/代码并保存
    changed = False
    for g in st.session_state[KEY_GROUPS]:
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
        save_groups(st.session_state[KEY_GROUPS])

    with_holding = st.checkbox("查询基金经理持有份额", value=True, help="来自基金中报/年报，首次查询每只基金约需 3~10 秒，之后走本地缓存")
    submitted = st.button("开始查询", type="primary", width="stretch")
    st.divider()
    st.caption(
        "**数据口径说明**\n\n"
        "- 基金规模：最新披露的期末净资产\n"
        "- 阶段涨幅：近1月/近3月/近6月/今年以来/近1年/近2年/近3年/近5年，来自天天基金"
        "（每日更新，新基金未满对应期数的显示 --，点表头按数值排序）\n"
        "- 同类排名(近1年/近3年/近5年)：对应期数涨幅在天天基金二级分类（如混合型-偏股）中的百分位，"
        "前1%=最好、前100%=最差，红字=前25%、绿字=后25%（每日更新，未满对应年数显示 --）\n"
        "- 经理持有份额：定期报告披露的区间（万份），"
        "一年最多更新两次（中报 8 月底前、年报次年 3 月底前）\n"
        "- 经理变更：近一年有新任/离任时，基金经理单元格琥珀色提示\n"
        "- 经理汇总：每组总览表下方可展开「本组经理汇总」，"
        "在管规模/从业年限来自经理目录快照（缓存 7 天）；"
        "再下方的经理按钮可下钻查看该经理全部在管基金（同「单只/经理查询」）\n\n"
        "查询结果缓存于 `.cache/`，基本信息 12 小时、持有份额 7 天后自动刷新"
    )

# ---------------- 查询（两段式） ----------------
# 第一帧（点击帧）：清掉上一轮结果，只渲染「正在启动查询」提示，然后 st.stop() 正常
# 收尾。本帧瞬时完成，旧的总览大表/警告条在帧结束时即被清理、从页面消失；若在同一
# 帧里直接跑长查询，查询的几十秒到几分钟里旧结果会以灰色（stale 半透明）状态滞留
# 整页，看起来像上一次查询的结果残留着没刷新
if submitted and not st.session_state.get(KEY_QUERY_PENDING):
    st.session_state[KEY_RESULTS] = None
    st.session_state[KEY_ERRORS] = None
    st.session_state[KEY_PLAN] = None
    st.session_state[KEY_DUP_CODES] = set()
    st.session_state[KEY_CODE_GROUPS] = {}
    st.session_state[KEY_DUP_WITHIN] = {}
    st.session_state[KEY_QUERY_PENDING] = True
    st.info("⏳ 正在启动查询…（若几秒后仍未开始，请再点一次“开始查询”）")
    # 注意用 components.html 而非 st.iframe：后者 height=0 时不渲染 iframe，回点不会发生
    components.html(_KICK_QUERY_HTML, height=0)
    st.stop()

# 第二帧（回点帧）：armed 状态下再次「开始查询」（iframe 自动回点或用户手点），
# 执行真正的查询
if submitted and st.session_state.pop(KEY_QUERY_PENDING, False):
    plan = [(g["name"], parse_codes(g["codes"])) for g in st.session_state[KEY_GROUPS]]
    # 统计跨分组重复：同一代码出现在几个分组
    code_groups = {}
    for gname, g_codes in plan:
        for c in g_codes:
            code_groups.setdefault(c, []).append(gname)
    st.session_state[KEY_DUP_CODES] = {c for c, gs in code_groups.items() if len(gs) > 1}
    st.session_state[KEY_CODE_GROUPS] = code_groups
    # 统计组内重复：同一分组里重复输入的代码（查询时仍合并为一条）
    dup_within = {}
    for g in st.session_state[KEY_GROUPS]:
        repeated = {c: n for c, n in count_codes(g["codes"]).items() if n > 1}
        if repeated:
            dup_within[g["name"]] = repeated
    st.session_state[KEY_DUP_WITHIN] = dup_within
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
            results, errors = query_all(all_codes, with_holding, status_box, progress_bar)
            progress_bar.empty()
            status_box.update(label="查询完成", state="complete", expanded=False)
        st.session_state[KEY_RESULTS] = results
        st.session_state[KEY_ERRORS] = errors
        st.session_state[KEY_PLAN] = plan
        st.session_state[KEY_WITH_HOLDING] = with_holding

# ---------------- 结果展示：分组标签页 ----------------
results = st.session_state.get(KEY_RESULTS)
if results is not None:
    disp = {c: display_row(r) for c, r in results.items()}  # 预警文案用展示行拼装
    if st.session_state.get(KEY_DUP_CODES):
        dup_desc = "、".join(
            f"`{c}`（{disp[c]['名称'] if c in disp else ''}）" for c in sorted(st.session_state[KEY_DUP_CODES])
        )
        st.warning(f"🔁 以下基金在多个分组中重复出现（表格中以琥珀色高亮显示）：{dup_desc}")
    dup_within = st.session_state.get(KEY_DUP_WITHIN) or {}
    if dup_within:
        desc = "；".join(
            f"{g}：" + "、".join(f"`{c}`×{n}" for c, n in codes.items())
            for g, codes in dup_within.items()
        )
        st.warning(f"✍️ 以下分组内重复输入的代码已自动合并为一条（同样高亮显示）：{desc}")
    small_nav = [c for c, r in results.items() if r.get("small")]
    if small_nav:
        small_desc = "、".join(
            f"{disp[c]['名称']}（`{c}`，{disp[c]['规模(净资产)']}）" for c in small_nav if c in disp
        )
        if small_desc:
            st.error(f"⚠️ 以下 {len(small_nav)} 只基金净资产低于 0.5 亿（迷你基金，规模单元格红色高亮，注意清盘风险）：{small_desc}")
    downs = {c: r["holding_chg"] for c, r in results.items() if str(r.get("holding_chg", "")).startswith("降")}
    ups = {c: r["holding_chg"] for c, r in results.items() if str(r.get("holding_chg", "")).startswith("升")}
    if downs:
        desc = "、".join(f"{disp[c]['名称']}（`{c}`，↓{v[1:]}）" for c, v in downs.items())
        st.warning(f"📉 以下 {len(downs)} 只基金的经理**减持**了本基金（较上一份中报/年报，单元格红色高亮）：{desc}")
    if ups:
        desc = "、".join(f"{disp[c]['名称']}（`{c}`，↑{v[1:]}）" for c, v in ups.items())
        st.success(f"📈 以下 {len(ups)} 只基金的经理**增持**了本基金（较上一份中报/年报，单元格绿色高亮）：{desc}")
    mgr_change = {c: r["mgr_chg"] for c, r in results.items() if r.get("mgr_chg")}
    if mgr_change:
        desc = "、".join(f"{disp[c]['名称']}（`{c}`，{v}）" for c, v in mgr_change.items())
        st.warning(f"🔁 以下 {len(mgr_change)} 只基金**近一年基金经理有变更**（基金经理单元格琥珀色高亮）：{desc}")
    plan = [(n, [c for c in cs if c not in (st.session_state.get(KEY_ERRORS) or {})])
            for n, cs in st.session_state.get(KEY_PLAN) or []]
    non_empty = [(n, cs) for n, cs in plan if cs]
    if len(non_empty) > 1:
        # key+on_change 让 tabs 变 stateful：选中分组存进后端会话（st.session_state），
        # 经理下钻等长 rerun 或前端组件重建后不再弹回第一组（无 key 时选中态只在
        # 前端 React state，组件重挂载即回到 default=第一个分组）
        tabs = st.tabs(
            [f"{n}（{len(cs)}只）" for n, cs in non_empty],
            key=KEY_GROUP_TABS,
            on_change="rerun",
        )
        for tab, (name, codes) in zip(tabs, non_empty):
            # stateful tabs 配套 tab.open：只渲染选中分组。切换分组会触发整页 rerun，
            # 若所有分组都渲染，rerun 要重画全部大表/详情/iframe，切换明显卡顿
            if not tab.open:
                continue
            with tab:
                render_group(name, codes, results)
    elif len(non_empty) == 1:
        render_group(non_empty[0][0], non_empty[0][1], results)
    else:
        st.error("所有代码都查询失败，请检查输入")
    errors = st.session_state.get(KEY_ERRORS) or {}
    if errors:
        st.subheader("⚠️ 未成功的代码")
        for code, msg in errors.items():
            st.error(f"`{code}`：{msg}")
else:
    st.info(
        "👈 在左侧分组中输入基金代码，点击“开始查询”。示例：005827（易方达蓝筹精选混合）、"
        "110022（易方达消费行业）。可用“＋添加分组”建立自己的分组，结果按分组分标签页展示。"
    )
