# -*- coding: utf-8 -*-
"""可点表头排序的表格组件（st.components.v1.html 内嵌实现）。

为什么不用 st.dataframe：它会丢掉 pandas Styler 的单元格底色（迷你基金/增减持高亮），
且排序按字符串比较（"10亿"会排在"200亿"前面）；st.table 则不支持排序。
内嵌 HTML + 少量原生 JS 可以两者兼得：
- 点表头排序 / 再点切换升降序，浏览器本地完成、即时响应；
- 排序键由调用方通过 sort_values 显式给出（从原始数据算数值，见 render.display_sort_values），
  未提供键的列退回单元格文本比较，""（无效值）恒沉底；
- 单元格底色高亮、文字可划选复制、随当前显示顺序导出 CSV。
"""
import html as _html
import re as _re

import pandas as pd
import streamlit as st

from ui.constants import TWO_LINE_HEADERS

# 高亮底色（与总览表一致的半透明色，浅/深色主题都可读）
_SMALL_BG = "rgba(229,57,53,0.45)"
_UP_BG = "rgba(46,160,67,0.40)"
_DOWN_BG = "rgba(229,57,53,0.45)"

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 12.5px/1.5 -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif; }
.tools { display: flex; justify-content: flex-end; margin: 2px 0 6px; }
.tools button { font: inherit; padding: 2px 12px; border-radius: 6px; cursor: pointer;
  border: 1px solid #b6bcc8; background: transparent; color: inherit; }
.wrap { overflow: auto; max-height: 620px; border-radius: 8px; }
table { border-collapse: separate; border-spacing: 0; width: 100%; }
th { position: sticky; top: 0; z-index: 1; cursor: pointer; user-select: none;
  white-space: nowrap; padding: 7px 9px; text-align: left; font-weight: 600;
  background: #eef1f5; color: #26313f; border-bottom: 2px solid #d4d9e0; }
th[data-col]:hover { background: #e2e7ee; }
th .arr { display: inline-block; width: 1.1em; color: #5a6b81; font-weight: 400; }
/* 分组表头（同类排名 跨 近1年/近3年 两列）：不可点、居中 */
th.grp { cursor: default; text-align: center; }
td { padding: 6px 9px; white-space: nowrap; border-bottom: 1px solid #e6e9ee; color: #26313f; }
td.small { background: __SMALL__; } td.up { background: __UP__; } td.down { background: __DOWN__; }
td.mgrchg { background: rgba(255,170,0,0.45); }
/* 阶段涨幅：正收益红字 / 负收益绿字（A 股习惯红涨绿跌，文字色而非底色） */
td.ret-up { color: #E53935; } td.ret-down { color: #2EA043; }
/* 行悬停底色不覆盖高亮单元格（红/绿/琥珀提示悬停时保持可见） */
tr:hover td:not(.small):not(.up):not(.down):not(.mgrchg) { background: rgba(120,140,170,0.10); }
@media (prefers-color-scheme: dark) {
  th { background: #212b3b; color: #dbe3ee; border-bottom-color: #364257; }
  th[data-col]:hover { background: #2a3648; }
  th .arr { color: #9db0ca; }
  td { color: #cfd7e3; border-bottom-color: #2c3646; }
  td.ret-up { color: #F85149; } td.ret-down { color: #3FB950; }
}
""".replace("__SMALL__", _SMALL_BG).replace("__UP__", _UP_BG).replace("__DOWN__", _DOWN_BG)

_JS = """
function sortTable(th) {
  const table = document.getElementById('t');
  const idx = parseInt(th.dataset.idx, 10);
  const asc = th.getAttribute('aria-sort') !== 'ascending';
  document.querySelectorAll('th[data-col]').forEach(h => {
    h.setAttribute('aria-sort', 'none');
    h.querySelector('.arr').textContent = '↕';
  });
  th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
  th.querySelector('.arr').textContent = asc ? '↑' : '↓';
  const key = r => parseFloat(r.cells[idx].dataset.sort);
  const txt = r => r.cells[idx].textContent.trim();
  const rows = Array.from(table.tBodies[0].rows);
  const valid = rows.filter(r => r.cells[idx].dataset.sort !== '');
  const empty = rows.filter(r => r.cells[idx].dataset.sort === '');
  valid.sort((a, b) => {
    const ka = key(a), kb = key(b);
    if (!isNaN(ka) && !isNaN(kb)) return asc ? ka - kb : kb - ka;
    return asc ? txt(a).localeCompare(txt(b), 'zh') : txt(b).localeCompare(txt(a), 'zh');
  });
  [...valid, ...empty].forEach(r => table.tBodies[0].appendChild(r));
}
function exportCSV() {
  const q = s => Array.from(document.querySelectorAll(s));
  const header = q('th[data-col]').map(th => '"' + th.dataset.col.replace(/"/g, '""') + '"');
  const lines = [header.join(',')];
  q('#t tbody tr').forEach(tr => {
    lines.push(Array.from(tr.cells).map(td => '"' + td.textContent.trim().replace(/"/g, '""') + '"').join(','));
  });
  const blob = new Blob(['\\ufeff' + lines.join('\\r\\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = document.title || 'fund_table.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}
// 两行表头：第二行 th 的 sticky 偏移 = 第一行实际渲染高度。必须量第一行 tr 元素自身——
// 第一行的普通列 th 带 rowspan=2，量单元格会得到两行总高，滚动时子表头会被顶到数据行上
(function () {
  const rows = document.querySelectorAll('#t thead tr');
  if (rows.length > 1) {
    const h = rows[0].getBoundingClientRect().height;
    rows[1].querySelectorAll('th').forEach(th => { th.style.top = h + 'px'; });
  }
})();
"""


# 「组名(子项)」形式的列名（如 同类排名(近1年)）；「规模(净资产)」这类单列不算组，见下方连续判定
_GROUPED_COL = _re.compile(r"^(?P<grp>[^(]+)\((?P<sub>[^)]+)\)$")


def _leaf_th(col, idx, label=None, rowspan=False):
    """可排序的叶子表头：label 缺省用完整列名；分组列只显示子项标签。
    data-col 始终存完整列名（CSV 导出表头与平铺列名一致）；data-idx 是 body 单元格列号，
    两行表头下叶子 th 在自己行内的位置不再等于列号，排序必须用显式索引。
    超长表头（TWO_LINE_HEADERS）在 <br> 处折成两行。"""
    if label is None and str(col) in TWO_LINE_HEADERS:
        a, b = TWO_LINE_HEADERS[str(col)]
        lab_html = f"{_html.escape(a)}<br>{_html.escape(b)}"
    else:
        lab_html = _html.escape(label if label is not None else str(col))
    span = ' rowspan="2"' if rowspan else ""
    return (f'<th{span} data-col="{_html.escape(str(col))}" data-idx="{idx}" aria-sort="none" '
            f'onclick="sortTable(this)">{lab_html}<span class="arr">↕</span></th>')


def _grouped_thead_html(cols):
    """列名清单 -> 两行表头：连续≥2列同名「组(子项)」时，第一行组名 colspan 合并、
    第二行列子项；其余列 rowspan=2 贯穿两行。无分组列时退回单行表头。"""
    row1, row2 = [], []
    has_group = False
    i = 0
    while i < len(cols):
        m = _GROUPED_COL.match(str(cols[i]))
        if m:
            j = i
            while j < len(cols) and (mj := _GROUPED_COL.match(str(cols[j]))) and mj.group("grp") == m.group("grp"):
                j += 1
            if j - i >= 2:  # 相邻且同组名≥2列才合并（单独的「规模(净资产)」保持原样）
                has_group = True
                row1.append(f'<th class="grp" colspan="{j - i}">{_html.escape(m.group("grp"))}</th>')
                for k in range(i, j):
                    row2.append(_leaf_th(cols[k], k, label=_GROUPED_COL.match(str(cols[k])).group("sub")))
                i = j
                continue
        row1.append(_leaf_th(cols[i], i, rowspan=True))
        row2.append("")
        i += 1
    if not has_group:
        return f"<thead><tr>{''.join(row1)}</tr></thead>"
    return f"<thead><tr>{''.join(row1)}</tr><tr>{''.join(row2)}</tr></thead>"


def sortable_table_html(df: pd.DataFrame, small_codes=None, mgr_change_codes=None,
                        sort_values=None, title="fund_table.csv") -> str:
    """生成可排序表格的完整 HTML（纯函数，便于测试）。
    small_codes：迷你基金代码集合，其「规模(净资产)」单元格标红；
    mgr_change_codes：近一年经理有变更的代码集合，其「基金经理」单元格标琥珀色；
    sort_values：{列名: [排序键…]}，长度须与 df 行数一致，键为数字或 None（沉底）；
    未提供键的列按单元格文本排序。含「组(子项)」相邻同组列时表头自动两行分组。"""
    small_codes = set(small_codes or ())
    mgr_change_codes = set(mgr_change_codes or ())
    sort_values = sort_values or {}
    cols = list(df.columns)
    thead = _grouped_thead_html(cols)
    trs = []
    for ri, (_, row) in enumerate(df.iterrows()):
        tds = []
        for c in cols:
            val = "" if pd.isna(row[c]) else str(row[c])
            col_keys = sort_values.get(c)
            if col_keys is not None and ri < len(col_keys):
                data_sort = "" if col_keys[ri] is None else str(col_keys[ri])
            else:
                data_sort = _html.escape(val, quote=True)
            cls = ""
            if c == "规模(净资产)" and str(row.get("代码")) in small_codes:
                cls = "small"
            elif c == "基金经理" and str(row.get("代码")) in mgr_change_codes:
                cls = "mgrchg"
            elif c in ("持有较上期", "较上期"):
                cls = "up" if val.startswith("↑") else "down" if val.startswith("↓") else ""
            elif str(c).startswith("同类排名("):
                # 同类排名各期：前25%红字（好）、后25%绿字（差）——按数值排序键判定，无键/未满期不着色
                kv = col_keys[ri] if col_keys is not None and ri < len(col_keys) else None
                cls = "ret-up" if kv is not None and kv <= 25 else "ret-down" if kv is not None and kv > 75 else ""
            elif c in ("近1月", "近3月", "近6月", "今年以来", "近1年", "近2年", "近3年", "近5年"):
                # 阶段涨幅：正绿字负红字（文字色），--（未满期/无数据）不着色
                cls = "ret-down" if val.startswith("-") and val != "--" else "ret-up" if val not in ("--", "") else ""
            tds.append(f'<td class="{cls}" data-sort="{data_sort}">{_html.escape(val)}</td>')
        trs.append("<tr>" + "".join(tds) + "</tr>")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head><body>"
        "<div class='tools'><button onclick='exportCSV()'>⬇️ 导出 CSV</button></div>"
        f"<div class='wrap'><table id='t'>{thead}"
        f"<tbody>{''.join(trs)}</tbody></table></div>"
        f"<script>{_JS}</script></body></html>"
    )


def render_sortable_table(df: pd.DataFrame, small_codes=None, mgr_change_codes=None,
                          sort_values=None, title="fund_table.csv"):
    """在 Streamlit 页面上渲染可点表头排序的表格"""
    n = len(df)
    height = min(120 + 36 * n, 700)
    st.iframe(sortable_table_html(df, small_codes, mgr_change_codes, sort_values, title), height=height)
