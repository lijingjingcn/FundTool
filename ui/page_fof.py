# -*- coding: utf-8 -*-
"""FOF 持仓分析页：全市场 FOF 的基金持仓穿透，反向统计每只基金被多少只 FOF 持有。

流程：fund_universe() 取全量代码表过滤 FOF 子类型 -> 按母基金去重（A/C 份额只查一份）
-> 逐只拉基金持仓（移动端接口全部持仓，缓存 7 天）-> 按被持基金母基金反向聚合。
首轮全市场约 700 只母基金 FOF、每只一次请求（限速 0.25s）约 5~8 分钟，之后走缓存秒开；
持仓季度更新，报告季后重跑即可拿到新数据。
"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fundtool.client import aggregate_fof_holdings, parent_fund_key  # noqa: E402
from ui.detail import render_fund_detail  # noqa: E402
from ui.state import KEY_FOF_RESULT, get_client  # noqa: E402

st.title("🧺 FOF 持仓分析")
st.caption(
    "全市场 FOF 的基金持仓穿透：每只基金被多少只 FOF 持有、谁在重仓它。"
    "持仓为最新定期报告口径（季度更新，本地缓存 7 天）；FOF 的 A/C 份额只算一只持有人；"
    "被持基金按母基金聚合（南方致远A/C 合并计数）。数据来自天天基金公开接口，仅供参考。"
)

# FOF 子类型（fundcode_search.js 的细分类型）：默认排除 QDII-FOF（持仓为海外基金，混入会干扰）
FOF_TYPES = ["FOF-稳健型", "FOF-均衡型", "FOF-进取型", "QDII-FOF"]
DEFAULT_TYPES = ["FOF-稳健型", "FOF-均衡型", "FOF-进取型"]


def dedupe_fof_universe(fof_list):
    """全市场 FOF（含 A/C 份额）-> 母基金去重：按简称的母基金键归组，每组取代码最小的一个
    代表（A/C 持仓相同，省一半请求）。返回 [{code,name,type}]。纯函数，便于测试。"""
    groups = {}
    for u in fof_list:
        groups.setdefault(parent_fund_key(u["name"]), []).append(u)
    return [{"code": min(g, key=lambda u: u["code"])["code"],
             "name": min(g, key=lambda u: u["code"])["name"],
             "type": g[0]["type"]}
            for g in groups.values()]


def fof_result_rows(agg, type_by_code):
    """聚合结果 -> 结果表展示行 + 排序键（纯函数，便于测试）。份额分布如 'A:3 C:2'。"""

    def _share_letter(name):
        s = str(name or "").rstrip()
        return s[-1] if s and s[-1].isascii() and s[-1].isalpha() and len(s) >= 5 else "基"

    rows, keys = [], []
    for r in agg:
        dist = {}
        for h in r["holders"]:
            for s in h["shares"]:
                letter = _share_letter(s["name"])
                dist[letter] = dist.get(letter, 0) + 1
        n = len(r["holders"])
        pcts = [h["pct"] for h in r["holders"] if h["pct"] is not None]
        rows.append({
            "母基金": r["key"],
            "代表份额": f"{r['rep_name']} {r['rep_code']}",
            "类型": type_by_code.get(r["rep_code"]) or "--",
            "被FOF持有数": n,
            "份额分布": " ".join(f"{k}:{v}" for k, v in sorted(dist.items())),
            "平均占比%": round(sum(pcts) / len(pcts), 2) if pcts else None,
            "最高占比%": max(pcts) if pcts else None,
        })
        keys.append(n)
    return rows, keys


# ---------------- 交互 ----------------
types_sel = st.multiselect(
    "FOF 子类型", FOF_TYPES, default=DEFAULT_TYPES,
    help="稳健型重仓债基、进取型重仓股基，分开看更有针对性；QDII-FOF 持仓为海外基金",
)
run = st.button("🧺 开始分析", disabled=not types_sel, type="primary")

client = get_client()
if run or (st.session_state.get(KEY_FOF_RESULT) or {}).get("types") == types_sel:
    if run:  # 重跑：逐只拉持仓（已缓存的秒过），按当前所选子类型重新聚合
        with st.status(f"正在分析 {len(types_sel)} 类 FOF 的基金持仓…") as stt:
            stt.update(label="正在获取全市场基金代码表…")
            fof_list = [u for u in client.fund_universe() if u["type"] in types_sel]
            fofs = dedupe_fof_universe(fof_list)
            stt.update(label=f"共 {len(fof_list)} 只 FOF（母基金去重后 {len(fofs)} 只），逐只拉取基金持仓…")
            fof_rows = []
            empty = 0
            prog = st.progress(0.0, text="拉取进度")
            for i, u in enumerate(fofs):
                stt.update(label=f"正在拉取 {i + 1}/{len(fofs)}：{u['name']}")
                try:
                    holdings = client.fof_holdings(u["code"])
                except Exception:  # 单只失败不致命，计为空持仓
                    holdings = []
                if not holdings:
                    empty += 1
                fof_rows.append({**u, "holdings": holdings})
                if (i + 1) % 10 == 0 or i + 1 == len(fofs):
                    prog.progress((i + 1) / len(fofs),
                                  text=f"拉取进度 {i + 1}/{len(fofs)}（{empty} 只无持仓数据）")
            agg = aggregate_fof_holdings(fof_rows)
            st.session_state[KEY_FOF_RESULT] = {"types": types_sel, "agg": agg,
                                                "n_fof": len(fofs), "n_empty": empty}
            stt.update(label=f"完成：{len(fofs)} 只 FOF、{len(agg)} 个被持母基金", state="complete", expanded=False)

    res = st.session_state[KEY_FOF_RESULT]
    agg = res["agg"]
    st.success(f"共分析 {res['n_fof']} 只 FOF（母基金去重，{res['n_empty']} 只无持仓数据），"
               f"持有 {len(agg)} 个母基金。点表格里任意一行，下方显示持有它的 FOF 明细。")
    type_by_code = {u["code"]: u["type"] for u in client.fund_universe()}
    rows, _ = fof_result_rows(agg, type_by_code)
    df = pd.DataFrame(rows)
    # 点行选中 -> 下方联动持有人明细；st.dataframe 自带列头排序与工具栏 CSV 下载，
    # 数值列保持原始类型（None -> NaN 空单元格）排序才正确
    event = st.dataframe(
        df, hide_index=True, use_container_width=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "平均占比%": st.column_config.NumberColumn(format="%.2f"),
            "最高占比%": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    sel_rows = (event.selection or {}).get("rows") or []
    if not sel_rows:
        st.info("👆 点击表格中的任意一行，查看该基金被哪些 FOF 持有")
    else:
        r = agg[sel_rows[0]]
        holders = r["holders"]
        # 该母基金涉及的全部份额（多个时提供 A/C 切换，分别查看持有人与详情）
        share_stat = {}  # 份额代码 -> {name, n(被几只FOF持有)}
        for h in holders:
            for s in h["shares"]:
                cur = share_stat.setdefault(s["code"], {"name": s["name"], "n": 0})
                cur["n"] += 1
        codes = sorted(share_stat, key=lambda c: -share_stat[c]["n"])
        if len(codes) > 1:
            labels = {c: f"{share_stat[c]['name']}（被{share_stat[c]['n']}只FOF持有）" for c in codes}
            sel_code = st.radio("选择份额级别", codes, format_func=lambda c: labels[c],
                                horizontal=True, key=f"fof_share_{r['rep_code']}")
        else:
            sel_code = codes[0]
        sel_name = share_stat[sel_code]["name"]
        sel_holders = [h for h in holders if any(s["code"] == sel_code for s in h["shares"])]
        # 先看基金详情（与分组查询/单只查询同款，ui.detail 共用组件），再看它被哪些 FOF 持有
        with st.expander(f"基金详情：{sel_name}（{sel_code}）", expanded=True):
            render_fund_detail(sel_code, with_holding=True)
        st.markdown(f"##### {sel_name}（{sel_code}）的持有人")
        st.caption(f"共 {len(sel_holders)} 只 FOF 持有该份额，按占 FOF 净值比例降序：")
        hrows = []
        for h in sel_holders:
            pct = next((s["pct"] for s in h["shares"] if s["code"] == sel_code), None)
            hrows.append({"FOF": h["fof_name"], "代码": h["fof_code"], "子类型": h["fof_type"],
                          "占该FOF净值%": pct if pct is not None else "--"})
        st.table(pd.DataFrame(hrows).style.hide(axis="index"))
else:
    st.info("选好 FOF 子类型后点「开始分析」。首轮全市场约 5~8 分钟（之后走本地缓存秒开）。")
