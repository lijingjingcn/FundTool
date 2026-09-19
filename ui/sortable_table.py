# -*- coding: utf-8 -*-
"""可点表头排序的表格组件（st.components.v1.html 内嵌实现）。

为什么不用 st.dataframe：它会丢掉 pandas Styler 的单元格底色（迷你基金/增减持高亮），
且排序按字符串比较（"10亿"会排在"200亿"前面）；st.table 则不支持排序。
内嵌 HTML + 少量原生 JS 可以两者兼得：
- 点表头排序 / 再点切换升降序，浏览器本地完成、即时响应；
- data-sort 属性携带语义排序键（规模数值、持有档位、升降档数），-- 等无效值恒沉底；
- 单元格底色高亮、文字可划选复制、随当前显示顺序导出 CSV。
"""
import html as _html
import re

import pandas as pd
import streamlit as st

# 语义排序键：把展示文案换算成可比较的数值
_SCALE_RE = re.compile(r"([\d.]+)\s*亿")
_HOLD_RANK = {"0": 0, "0-10": 1, "10-50": 2, "50-100": 3, ">100": 4}


def _scale_key(text):
    m = _SCALE_RE.search(str(text))
    return float(m.group(1)) if m else None


def _hold_key(text):
    v = str(text).replace("万份", "").strip()
    if "未持有" in v:
        return 0
    v = v.replace("~", "-").replace("～", "-").replace("—", "-").replace("至", "-")
    return _HOLD_RANK.get(v)


def _change_key(text):
    v = str(text)
    m = re.search(r"\d+", v)
    if v.startswith("↑"):
        return int(m.group()) if m else None
    if v.startswith("↓"):
        return -int(m.group()) if m else None
    return 0 if v == "→持平" else None


_COLUMN_KEYS = {"规模(净资产)": _scale_key, "基金经理持有本基金": _hold_key, "持有较上期": _change_key}

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
th:hover { background: #e2e7ee; }
th .arr { display: inline-block; width: 1.1em; color: #5a6b81; font-weight: 400; }
td { padding: 6px 9px; white-space: nowrap; border-bottom: 1px solid #e6e9ee; color: #26313f; }
td.small { background: __SMALL__; } td.up { background: __UP__; } td.down { background: __DOWN__; }
td.mgrchg { background: rgba(255,170,0,0.45); }
/* 行悬停底色不覆盖高亮单元格（红/绿/琥珀提示悬停时保持可见） */
tr:hover td:not(.small):not(.up):not(.down):not(.mgrchg) { background: rgba(120,140,170,0.10); }
@media (prefers-color-scheme: dark) {
  th { background: #212b3b; color: #dbe3ee; border-bottom-color: #364257; }
  th:hover { background: #2a3648; }
  th .arr { color: #9db0ca; }
  td { color: #cfd7e3; border-bottom-color: #2c3646; }
}
""".replace("__SMALL__", _SMALL_BG).replace("__UP__", _UP_BG).replace("__DOWN__", _DOWN_BG)

_JS = """
function sortTable(th) {
  const table = document.getElementById('t');
  const idx = Array.from(th.parentNode.children).indexOf(th);
  const asc = th.getAttribute('aria-sort') !== 'ascending';
  document.querySelectorAll('th').forEach(h => {
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
  const header = q('th').map(th => '"' + th.dataset.col.replace(/"/g, '""') + '"');
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
"""


def sortable_table_html(df: pd.DataFrame, small_codes=None, mgr_change_codes=None,
                         title="fund_table.csv") -> str:
    """生成可排序表格的完整 HTML（纯函数，便于测试）。
    small_codes：迷你基金代码集合，其「规模(净资产)」单元格标红；
    mgr_change_codes：近半年经理有变更的代码集合，其「基金经理」单元格标琥珀色。"""
    small_codes = set(small_codes or ())
    mgr_change_codes = set(mgr_change_codes or ())
    cols = list(df.columns)
    ths = "".join(
        f'<th data-col="{_html.escape(str(c))}" aria-sort="none" onclick="sortTable(this)">'
        f'{_html.escape(str(c))}<span class="arr">↕</span></th>'
        for c in cols
    )
    trs = []
    for _, row in df.iterrows():
        tds = []
        for c in cols:
            val = "" if pd.isna(row[c]) else str(row[c])
            keyf = _COLUMN_KEYS.get(c)
            sort_key = keyf(val) if keyf else None
            data_sort = "" if sort_key is None and keyf else _html.escape(val, quote=True)
            if sort_key is not None:
                data_sort = str(sort_key)
            cls = ""
            if c == "规模(净资产)" and str(row.get("代码")) in small_codes:
                cls = "small"
            elif c == "基金经理" and str(row.get("代码")) in mgr_change_codes:
                cls = "mgrchg"
            elif c in ("持有较上期", "较上期"):
                cls = "up" if val.startswith("↑") else "down" if val.startswith("↓") else ""
            tds.append(f'<td class="{cls}" data-sort="{data_sort}">{_html.escape(val)}</td>')
        trs.append("<tr>" + "".join(tds) + "</tr>")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head><body>"
        "<div class='tools'><button onclick='exportCSV()'>⬇️ 导出 CSV</button></div>"
        f"<div class='wrap'><table id='t'><thead><tr>{ths}</tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></div>"
        f"<script>{_JS}</script></body></html>"
    )


def render_sortable_table(df: pd.DataFrame, small_codes=None, mgr_change_codes=None, title="fund_table.csv"):
    """在 Streamlit 页面上渲染可点表头排序的表格"""
    n = len(df)
    height = min(120 + 36 * n, 700)
    st.iframe(sortable_table_html(df, small_codes, mgr_change_codes, title), height=height)
