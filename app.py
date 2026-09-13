# -*- coding: utf-8 -*-
"""基金信息查询工具 —— Streamlit GUI

展示：基金名称、基金经理、基金经理持有本基金的份额（来自定期报告）、基金规模。
支持分组管理与本地持久化：输入内容保存在「我的基金.json」，下次启动直接查询。
数据来源：天天基金（东方财富）公开接口 + 基金定期报告 PDF。
"""
import json
import os
import re
import time
import uuid

import pandas as pd
import streamlit as st

from fundtool import EastFundClient, FundApiError, JsonCache, format_range, get_manager_holding

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 测试通过 FUNDTOOL_DATA_FILE 指向临时文件，避免冒烟测试覆盖真实分组数据
DATA_FILE = os.environ.get("FUNDTOOL_DATA_FILE") or os.path.join(BASE_DIR, "我的基金.json")
# 查询不设总量上限：全部代码自动分批处理，批次之间稍作停顿以免请求过密。
# 首次查询每只需下载解析报告PDF（约2~5秒/只），已查过的走缓存。
BATCH_SIZE = 30
BATCH_PAUSE = 8  # 批间停顿秒数
# 迷你基金阈值（元）：净资产低于 5000 万的基金有清盘风险，界面特殊提醒
SMALL_NAV_YUAN = 0.5e8

st.set_page_config(page_title="基金信息查询工具", page_icon="📊", layout="wide")

st.title("📊 基金信息查询工具")
st.caption(
    "按分组输入基金代码，查询：基金名称 · 基金经理 · 基金经理持有本基金份额（定期报告披露）· 基金规模。"
    "输入自动保存在本地，下次启动直接查询。数据来自天天基金公开数据与基金定期报告，仅供参考。"
)


@st.cache_resource
def get_client():
    return EastFundClient(JsonCache(os.path.join(BASE_DIR, ".cache")))


# ---------------- 分组：本地持久化 ----------------
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


if "groups" not in st.session_state:
    st.session_state.groups = load_groups()


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


def query_all(codes, with_holding, status_box, progress_bar):
    """逐只查询，自动分批直到全部完成。返回 {代码: 行数据}、{代码: 错误}、{代码: 规模文案（迷你基金）}"""
    results, errors, small_nav = {}, {}, {}
    total = len(codes)
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    for i, code in enumerate(codes):
        if i > 0 and i % BATCH_SIZE == 0:
            status_box.update(label=f"第 {i // BATCH_SIZE}/{n_batches} 批完成，批间停顿 {BATCH_PAUSE} 秒…")
            time.sleep(BATCH_PAUSE)
        try:
            info = get_client().basic_info(code)
            if info is None:
                errors[code] = "未找到该基金（代码不存在或已清盘）"
                continue
            nav = nav_yuan(info.get("ENDNAV"))
            scale = yi(info.get("ENDNAV"))
            if nav is not None and nav < SMALL_NAV_YUAN:
                scale += " ⚠️"
                small_nav[code] = scale
            row = {"代码": code, "名称": info.get("SHORTNAME", "--"), "类型": info.get("FTYPE", "--"),
                   "基金经理": (info.get("JJJL") or "--").replace(",", "、"),
                   "规模(净资产)": scale,
                   "规模日期": (info.get("FEGMRQ") or "--").replace(" 00:00:00", ""),
                   "净值日期": info.get("FSRQ", "--")}
            if with_holding:
                hold = get_manager_holding(get_client(), code)
                if hold["status"] == "ok":
                    tag = "中报" if "中期" in hold.get("report_title", "") else "年报"
                    rng = format_range(hold.get("manager_range"))
                    row["基金经理持有本基金"] = rng or "见报告原文"
                    row["持有数据来源"] = f"{hold.get('report_date', '')}{tag}"
                else:
                    row["基金经理持有本基金"] = "--"
                    row["持有数据来源"] = hold.get("error", "获取失败")
            results[code] = row
        except FundApiError as e:
            errors[code] = str(e)
        status_box.update(label=f"正在查询第 {i + 1}/{total} 只（第 {i // BATCH_SIZE + 1}/{n_batches} 批）")
        progress_bar.progress((i + 1) / total, text=f"查询进度 {i + 1}/{total}（共 {n_batches} 批）")
    return results, errors, small_nav


def render_fund_detail(code):
    """单只基金的详情（在 expander 内调用）"""
    info = get_client().basic_info(code)
    if info is None:
        st.warning("基本信息不可用")
        return
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
    if st.session_state.get("with_holding"):
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
        else:
            st.warning(f"未能获取：{hold.get('error')}")
    tenure = get_client().manager_tenure(code)
    if tenure:
        st.markdown("**基金经理任职情况**")
        tdf = pd.DataFrame(
            [
                {"基金经理": "、".join(t["managers"]), "起始": t["start"], "截止": t["end"],
                 "任职天数": t["days"], "任职回报": f"{t['return_pct']}%"}
                for t in tenure
            ]
        )
        st.table(tdf.style.hide(axis="index"))


# 跨分组重复行的底色（半透明琥珀色，深浅主题下都可读）
_DUP_BG = "background-color: rgba(255,170,0,0.32)"
# 迷你基金（净资产<0.5亿）规模单元格的底色（半透明红色）
_SMALL_BG = "background-color: rgba(229,57,53,0.45)"


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
        # 用静态 HTML 表格渲染（st.dataframe 是画布渲染，文字无法鼠标划选复制）
        styler = df.style.apply(_highlight_dup_rows(highlight), axis=1)
        if small_here:
            styler = styler.apply(_highlight_small_scale(small_here, df["代码"]), axis=0)
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
        "一年最多更新两次（中报 8 月底前、年报次年 3 月底前）\n\n"
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
            results, errors, small_nav = query_all(all_codes, with_holding, status_box, progress_bar)
            progress_bar.empty()
            status_box.update(label="查询完成", state="complete", expanded=False)
        st.session_state["results"] = results
        st.session_state["errors"] = errors
        st.session_state["small_nav"] = small_nav
        st.session_state["plan"] = plan
        st.session_state["with_holding"] = with_holding

# ---------------- 结果展示：分组标签页 ----------------
results = st.session_state.get("results")
if results is not None:
    dup_codes = st.session_state.get("dup_codes") or set()
    code_groups = st.session_state.get("code_groups") or {}
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
