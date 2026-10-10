# -*- coding: utf-8 -*-
"""全市场筛选页：按「基金管理人所有从业人员持有本基金」占比区间筛选基金。

两阶段：
1. 初筛——天天基金 F10 内部持有比例（= 从业人员持有，按份额级别分母）。
   级别分母 ≤ 全基金分母，按区间下限初筛是合计口径的超集，不会漏；
2. 复核——命中基金下载最新中报/年报 PDF，取合计行（全基金口径）精确占比，
   再按 [下限, 上限] 过滤。可勾选跳过复核（快，但为份额级别口径）。

所有请求结果进本地缓存（持有人结构 7 天、报告解析永久），中断重跑只补未完成的；
报告每半年更新一次，报告季后重筛即可拿到新数据。
"""
import os
import re
import sys
import time

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fundtool import FundApiError, format_range, get_manager_holding  # noqa: E402
from ui.query import emp_nums  # noqa: E402
from ui.render import render_sortable_table  # noqa: E402
from ui.state import (  # noqa: E402
    KEY_GROUPS,
    KEY_SCREEN_PENDING,
    KEY_SCREEN_RESULT,
    get_client,
    new_group,
    save_groups,
)

# 类型桶（fundcode_search.js 的细分类型 -> 展示口径的勾选项）：混合型五个子类独立可选
TYPE_BUCKETS = {
    "混合型-偏股": ("混合型-偏股",),
    "混合型-偏债": ("混合型-偏债",),
    "混合型-灵活": ("混合型-灵活",),
    "混合型-平衡": ("混合型-平衡",),
    "混合型-绝对收益": ("混合型-绝对收益",),
    "股票型": ("股票型",),
    "指数型-股票": ("指数型-股票",),
    "QDII（股票/混合）": ("QDII-普通股票", "QDII-混合偏股", "QDII-混合平衡", "QDII-混合灵活"),
}
DEFAULT_BUCKETS = ["混合型-偏股", "混合型-偏债", "混合型-灵活", "混合型-平衡", "混合型-绝对收益", "股票型"]


def bucket_of(ftype):
    """细分类型 -> 所属类型桶名（不在任何桶返回 None）"""
    for bucket, types in TYPE_BUCKETS.items():
        if ftype in types:
            return bucket
    return None


def merge_class_rows(rows):
    """复核行按报告公告 ID 合并份额级别：同一基金 A/C 都命中时并成一行，
    代码列空格分隔、其余字段保留占比最高级别的值。纯函数，便于测试。"""
    final, seen_art = [], set()
    for r in sorted(rows, key=lambda x: -x["emp_pct"]):
        if r["art"] and r["art"] in seen_art:
            for f0 in final:
                if f0["art"] == r["art"]:
                    f0["codes"] += f" {r['code']}"
            continue
        seen_art.add(r["art"])
        r = dict(r)
        r["codes"] = r.pop("code")
        final.append(r)
    return final


# 点击帧的自动回点脚本（同分组页：第一帧清旧结果，iframe 延时回点按钮进第二帧）
_KICK_SCREEN_HTML = """
<script>
  var fired = false;
  function kick() {
    if (fired) { return; }
    try {
      var btns = window.parent.document.querySelectorAll('button');
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].textContent.indexOf('开始筛选') >= 0) {
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


def _fmt_pct(p):
    return f"{p:.4f}".rstrip("0").rstrip(".")


st.title("🌐 全市场筛选（员工持有）")
st.caption(
    "在所选类型的**全部基金**中，筛选「基金管理人所有从业人员持有本基金」（基金公司员工持有）"
    "占比落在区间的基金。先逐只查天天基金 F10 内部持有比例（约 45 分钟，已扫的走缓存），"
    "再对命中基金下载定期报告 PDF 复核**全基金合计口径**的精确占比（每只约 5~10 秒）。"
    "报告每半年更新（中报 8 月底 / 年报次年 3 月底），报告季后重筛即可。"
)

with st.sidebar:
    st.header("🔎 筛选条件")
    c_lo, c_hi = st.columns(2)
    lo = c_lo.number_input("员工持有 ≥（%）", min_value=0.0, max_value=100.0, value=2.0, step=0.1)
    hi = c_hi.number_input("且 ≤（%）", min_value=0.0, max_value=100.0, value=100.0, step=0.1)
    buckets = st.multiselect(
        "基金类型",
        list(TYPE_BUCKETS),
        default=DEFAULT_BUCKETS,
        help="混合型五个子类（偏股/偏债/灵活/平衡/绝对收益）可单独或组合勾选；默认全选混合+股票型",
    )
    fast_mode = st.checkbox(
        "跳过 PDF 复核（快）",
        value=False,
        help="只用 F10 内部持有比例（按份额级别口径，分级基金会偏高），不下载报告 PDF。"
        "适合宽区间快速摸底；精确名单请保持勾掉",
    )
    submitted = st.button("开始筛选", type="primary", width="stretch")
    st.divider()
    st.caption(
        "**口径说明**\n\n"
        "- F10「内部持有比例」= 基金管理公司从业人员持有，按**份额级别**份额为分母；\n"
        "- 报告 PDF「合计」行 = 全基金总份额为分母，为最终口径；\n"
        "- 级别分母 ≤ 合计分母，故按下限初筛不会漏；区间上限在复核后过滤\n\n"
        "筛选结果缓存于 `.cache/`（持有人结构 7 天），中断后重点「开始筛选」自动续跑"
    )

# ---------------- 两段式执行（同分组查询页：点击帧清旧结果并 armed） ----------------
if submitted and not st.session_state.get(KEY_SCREEN_PENDING):
    st.session_state[KEY_SCREEN_RESULT] = None
    st.session_state[KEY_SCREEN_PENDING] = True
    st.info("⏳ 正在启动筛选…（若几秒后仍未开始，请再点一次“开始筛选”）")
    components.html(_KICK_SCREEN_HTML, height=0)
    st.stop()

if submitted and st.session_state.pop(KEY_SCREEN_PENDING, False):
    if hi < lo:
        st.warning("区间上限不能小于下限，请调整后重试")
        st.stop()
    if not buckets:
        st.warning("请至少选择一种基金类型")
        st.stop()
    universe = [r for r in get_client().fund_universe() if bucket_of(r["type"]) in buckets]
    n_uni = len(universe)
    if lo < 1.0 and not fast_mode:
        st.info(f"ℹ️ 下限 {lo}% 较低，命中基金可能较多，PDF 复核阶段会较久（每只约 5~10 秒）；"
                "中断后重点「开始筛选」自动续跑")
    t0 = time.time()
    with st.status(f"[1/2] 正在逐只查询 F10 内部持有比例（共 {n_uni} 只，首轮约 45 分钟，已扫走缓存）…",
                   expanded=True) as status_box:
        bar = st.progress(0.0, text="准备初筛…")
        hits, failed1 = [], []
        for i, r in enumerate(universe):
            try:
                h = get_client().holder_structure(r["code"])
            except FundApiError:
                failed1.append(r["code"])
                h = {}
            try:
                v = float(h.get("internal", "").replace("%", ""))
            except ValueError:
                v = None
            if v is not None and v >= lo:
                hits.append({**r, "internal": h.get("internal"), "date": h.get("date")})
            if (i + 1) % 50 == 0 or i + 1 == n_uni:
                el = time.time() - t0
                eta = el / (i + 1) * (n_uni - i - 1)
                bar.progress((i + 1) / n_uni,
                             text=f"初筛 {i + 1}/{n_uni} · 命中 {len(hits)} · "
                                  f"已用 {el / 60:.1f} 分 · 预计还需 {eta / 60:.1f} 分")
        status_box.update(label=f"[1/2] 初筛完成：{len(hits)} 个代码命中（网络失败 {len(failed1)}，"
                                "重筛可补）", state="complete", expanded=False)
    if not hits:
        st.warning(f"初筛无命中（员工持有 ≥ {lo}%）：所选类型共 {n_uni} 只都低于下限")
        st.stop()

    rows = []
    if fast_mode:
        rows = [{"codes": h["code"], "name": h["name"], "type": h["type"],
                 "emp_pct": float(h["internal"].replace("%", "")), "emp_shares": None,
                 "manager_range": "", "report": f"F10级别口径 {h.get('date', '')}",
                 "art": ""} for h in hits]
        rows = sorted(rows, key=lambda x: -x["emp_pct"])
    else:
        with st.status(f"[2/2] 正在下载 {len(hits)} 个命中代码的定期报告 PDF，按合计口径复核…",
                       expanded=True) as status_box:
            bar2 = st.progress(0.0, text="准备复核…")
            verified, failed2 = [], []
            t1 = time.time()
            for i, h in enumerate(hits):
                try:
                    hold = get_manager_holding(get_client(), h["code"])
                except Exception:  # noqa: BLE001 —— 单只失败不拖垮整批
                    failed2.append(h["code"])
                    hold = None
                if hold and hold.get("status") == "ok":
                    shares, pct = emp_nums(hold.get("employees_exact"))
                    if pct is not None and lo <= pct <= hi:
                        verified.append({
                            "code": h["code"], "name": h["name"], "type": h["type"],
                            "emp_pct": pct, "emp_shares": shares,
                            "manager_range": hold.get("manager_range") or "",
                            "report": hold.get("report_date", "") +
                                      ("中报" if "中期" in hold.get("report_title", "") else "年报"),
                            "art": (re.search(r"(AN\d+)", hold.get("pdf_url", "")) or [None, ""])[1],
                        })
                if (i + 1) % 20 == 0 or i + 1 == len(hits):
                    el = time.time() - t1
                    eta = el / (i + 1) * (len(hits) - i - 1)
                    bar2.progress((i + 1) / len(hits),
                                  text=f"复核 {i + 1}/{len(hits)} · 确认 {len(verified)} · "
                                       f"已用 {el / 60:.1f} 分 · 预计还需 {eta / 60:.1f} 分")
            rows = merge_class_rows(verified)
            status_box.update(label=f"[2/2] 复核完成：{len(verified)} 个代码 / {len(rows)} 只基金"
                                    f"（区间 {lo}%~{hi}%，失败 {len(failed2)}）",
                              state="complete", expanded=False)
    st.session_state[KEY_SCREEN_RESULT] = {"rows": rows, "lo": lo, "hi": hi}

# ---------------- 结果展示 ----------------
result = st.session_state.get(KEY_SCREEN_RESULT)
if result is not None:
    rows, lo, hi = result["rows"], result["lo"], result["hi"]
    if not rows:
        st.info(f"筛选完成：初筛命中的基金经报告 PDF 合计口径复核后，没有落在 {lo}%~{hi}% 区间内的"
                "（分级基金按份额级别口径会偏高，属正常）")
        st.stop()
    caliber = "F10 级别口径" if rows[0].get("emp_shares") is None else "报告 PDF 合计口径"
    m1, m2, m3 = st.columns(3)
    m1.metric("命中基金", f"{len(rows)} 只")
    m2.metric("口径", caliber)
    m3.metric("最高占比", f"{rows[0]['emp_pct']:.2f}%")
    disp = pd.DataFrame([{
        "代码": r["codes"], "名称": r["name"], "类型": r["type"],
        "员工持有%": _fmt_pct(r["emp_pct"]),
        "员工持有(万份)": f"{r['emp_shares'] / 1e4:,.2f}" if r.get("emp_shares") else "--",
        "经理持有": format_range(r.get("manager_range")) or "--",
        "报告期": r["report"],
    } for r in rows])
    st.caption("📊 点击表头排序，再点一次切换升/降序（-- 沉底）；导出的 CSV 与当前显示顺序一致")
    render_sortable_table(
        disp,
        sort_values={"员工持有%": [r["emp_pct"] for r in rows],
                     "员工持有(万份)": [r.get("emp_shares") for r in rows]},
        title=f"员工持有{_fmt_pct(lo)}-{_fmt_pct(hi)}%筛选.csv",
    )
    if caliber == "报告 PDF 合计口径":
        csv_bytes = disp.to_csv(index=False).encode("utf-8-sig")
        d1, d2 = st.columns(2)
        d1.download_button("⬇️ 导出 CSV", csv_bytes,
                           file_name=f"员工持有{_fmt_pct(lo)}-{_fmt_pct(hi)}%筛选结果.csv",
                           mime="text/csv")
        if d2.button("➕ 加入到分组查询", help="把命中基金代码写入「分组查询」页的一个新分组，可直接开始查询"):
            groups = st.session_state.get(KEY_GROUPS) or []
            g = new_group(f"员工持有{_fmt_pct(lo)}-{_fmt_pct(hi)}%")
            g["codes"] = " ".join(r["codes"] for r in rows)
            groups.append(g)
            st.session_state[KEY_GROUPS] = groups
            save_groups(groups)
            st.toast(f"已加入分组「{g['name']}」（{len(rows)} 只），到 📚 分组查询页查看", icon="✅")
