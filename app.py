# -*- coding: utf-8 -*-
"""基金信息查询工具 —— Streamlit GUI

展示：基金名称、基金经理、基金经理持有本基金的份额（来自定期报告）、基金规模。
支持分组管理与本地持久化：输入内容保存在「我的基金.json」，下次启动直接查询。
数据来源：天天基金（东方财富）公开接口 + 基金定期报告 PDF。
"""
import json
import os
import re
import uuid

import pandas as pd
import streamlit as st

from fundtool import EastFundClient, FundApiError, JsonCache, format_range, get_manager_holding

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "我的基金.json")
MAX_CODES = 60

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


def yi(value, unit="亿"):
    try:
        return f"{float(value) / 1e8:.2f}{unit}"
    except (TypeError, ValueError):
        return "--"


def query_all(codes, with_holding, status_box, progress_bar):
    """逐只查询，带进度显示。返回 {代码: 行数据} 与 {代码: 错误}"""
    results, errors = {}, {}
    total = len(codes)
    for i, code in enumerate(codes):
        try:
            info = get_client().basic_info(code)
            if info is None:
                errors[code] = "未找到该基金（代码不存在或已清盘）"
                continue
            row = {"代码": code, "名称": info.get("SHORTNAME", "--"), "类型": info.get("FTYPE", "--"),
                   "基金经理": (info.get("JJJL") or "--").replace(",", "、"),
                   "规模(净资产)": yi(info.get("ENDNAV")),
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
        status_box.update(label=f"正在查询第 {i + 1}/{total} 只…")
        progress_bar.progress((i + 1) / total, text=f"查询进度 {i + 1}/{total}")
    return results, errors


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
        st.dataframe(tdf, width="stretch", hide_index=True)


def render_group(name, codes, results):
    """一个分组的结果：总览表 + 每只基金详情"""
    rows = [results[c] for c in codes if c in results]
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", hide_index=True, height=max(220, 40 * len(df)))
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
            with st.expander(f"{results[code]['名称']}（{code}）"):
                render_fund_detail(code)


# ---------------- 侧边栏：分组输入 ----------------
def _delete_group(gid):
    st.session_state.groups = [g for g in st.session_state.groups if g["id"] != gid]
    save_groups(st.session_state.groups)


with st.sidebar:
    st.header("🔎 查询")
    st.caption("输入自动保存到本地（我的基金.json），下次启动直接点“开始查询”")
    for g in st.session_state.groups:
        gid = g["id"]
        c_name, c_del = st.columns([5, 1])
        c_name.text_input("分组名", value=g["name"], key=f"name_{gid}", label_visibility="collapsed")
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
        if len(all_codes) > MAX_CODES:
            all_codes = all_codes[:MAX_CODES]
            st.info(f"一次最多查询 {MAX_CODES} 只，已截取前 {MAX_CODES} 个")
        with st.status(f"正在查询 {len(all_codes)} 只基金…", expanded=True) as status_box:
            progress_bar = st.progress(0.0, text="准备查询…")
            results, errors = query_all(all_codes, with_holding, status_box, progress_bar)
            progress_bar.empty()
            status_box.update(label="查询完成", state="complete", expanded=False)
        st.session_state["results"] = results
        st.session_state["errors"] = errors
        st.session_state["plan"] = plan
        st.session_state["with_holding"] = with_holding

# ---------------- 结果展示：分组标签页 ----------------
results = st.session_state.get("results")
if results is not None:
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
