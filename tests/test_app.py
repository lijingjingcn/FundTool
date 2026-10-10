# -*- coding: utf-8 -*-
"""端到端冒烟测试（Streamlit AppTest，无需浏览器）+ 纯函数单测。

用法: python -m pytest tests/test_app.py -v    （或 python tests/test_app.py）
会真实调用数据接口/缓存，首次运行需要联网。

每个测试独立准备自己的分组数据与查询历史（写入临时 DATA_FILE，绝不触碰
真实的 我的基金.json），测试之间互不依赖、可单独运行。

通过 FUNDTOOL_DATA_FILE / FUNDTOOL_HISTORY_FILE 把数据文件指向临时目录。
"""
import io
import json
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["FUNDTOOL_DATA_FILE"] = os.path.join(tempfile.mkdtemp(prefix="fundtool_test_"), "我的基金.json")
os.environ["FUNDTOOL_HISTORY_FILE"] = os.path.join(os.path.dirname(os.environ["FUNDTOOL_DATA_FILE"]), "查询历史.json")
DATA_FILE = os.environ["FUNDTOOL_DATA_FILE"]
HISTORY_FILE = os.environ["FUNDTOOL_HISTORY_FILE"]

import pytest  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from ui.query import (  # noqa: E402
    build_mgr_dir_index,
    fund_overview_row,
    group_manager_agg,
    mgr_change_label,
)
from ui.render import display_row, fmt_employee_holding, manager_summary_rows, peer_pct, pct  # noqa: E402
from ui.state import (  # noqa: E402
    KEY_DETAIL_MANAGER,
    KEY_DETAIL_PAGE,
    KEY_DETAIL_SCOPE,
    KEY_GROUP_TABS,
    KEY_SINGLE_CODE,
    clear_history,
    get_client,
)
from ui.sortable_table import sortable_table_html  # noqa: E402
from ui.constants import TWO_LINE_HEADERS  # noqa: E402
from fundtool.client import parse_jdzf_ranks, parse_period_returns  # noqa: E402

import pandas as pd  # noqa: E402

# 多页应用：入口只做导航，各页面脚本可独立运行（AppTest 直接跑页面脚本）
GROUPS_PAGE = os.path.join(ROOT, "ui", "page_groups.py")
SINGLE_PAGE = os.path.join(ROOT, "ui", "page_single.py")
MACRO_PAGE = os.path.join(ROOT, "ui", "page_macro.py")


# ---------------- 公共工具 ----------------
def write_groups(groups):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _independent_local_data():
    """每个测试从干净状态开始：单分组空白数据 + 空历史（保证场景独立）"""
    write_groups([{"id": "test0001", "name": "我的基金", "codes": ""}])
    clear_history()
    yield


def click_query(at):
    """点击「开始查询」的完整两段式：第一帧 armed（清旧结果、正常收尾），
    第二帧（页面里由 iframe 自动回点，这里模拟为再点一次）执行查询"""
    next(b for b in at.button if "开始查询" in b.label).click().run()
    next(b for b in at.button if "开始查询" in b.label).click().run()


def overview_dfs(at):
    """只取分组总览表（以“代码”列为特征），排除详情里的任职表等。

    总览表用 st.table 渲染（文字可复制）；带高亮时传入 pandas Styler，
    AppTest 取到的 .value 已是底层 DataFrame。两行表头的 MultiIndex 列名
    在这里展平回 '同类排名(近1年)' 形式，方便下游断言按平铺列名访问。
    """
    out = []
    for d in list(at.dataframe) + list(at.table):
        v = d.value
        if hasattr(v, "data"):  # pandas Styler
            v = v.data
        if hasattr(v, "columns"):
            if isinstance(v.columns, pd.MultiIndex):
                rev = {pair: flat for flat, pair in TWO_LINE_HEADERS.items()}
                v = v.copy()
                v.columns = [rev.get((a, b), f"{a}({b})" if a else str(b)) for a, b in v.columns]
            if "代码" in v.columns:
                out.append(v)
    return out


def query_groups(codes_texts):
    """构造分组数据并查询：codes_texts = [(分组名, 代码文本), ...]，返回查询完成的 AppTest"""
    write_groups([{"id": f"test{i:04d}", "name": name, "codes": codes}
                  for i, (name, codes) in enumerate(codes_texts)])
    at = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at.run()
    click_query(at)
    return at


def switch_group(at, label):
    """切换分组标签页（stateful tabs 只渲染选中分组）：预置选中标签后重跑页面脚本"""
    at.session_state[KEY_GROUP_TABS] = label
    at.run()


# ---------------- 分组查询页 ----------------
def test_group_query_basic():
    """单分组查询：组内重复合并、无效代码报错、经理变更提示、详情含历史对比表"""
    at = query_groups([("我的基金", "005827 005827 999999")])
    dfs = overview_dfs(at)
    assert dfs, "总览表格未渲染"
    df = dfs[0]
    assert list(df["代码"]) == ["005827"], "组内重复应合并为一条"
    row = df.iloc[0]
    assert row["名称"] == "易方达蓝筹精选混合", row["名称"]
    assert row["基金经理持有本基金"] == ">100万份", row["基金经理持有本基金"]
    assert row["从业人员持有本基金"] == "854.48万份(0.0639%)", row["从业人员持有本基金"]
    assert any("999999" in e.value for e in at.error), [e.value for e in at.error]
    assert "持有较上期" in df.columns, df.columns
    assert re.fullmatch(r"(↑\d+档|↓\d+档|→持平|--)", row["持有较上期"]), row["持有较上期"]
    tables = [t.value.data if hasattr(t.value, "data") else t.value for t in at.table]
    assert any("报告期" in list(v.columns) for v in tables if hasattr(v, "columns")), "详情页应有历史对比表"
    warn_texts = [w.value for w in at.warning]
    assert any("005827" in w and "合并" in w for w in warn_texts), warn_texts
    # 近一年经理变更提示（005827 于 2026-05-23 新增共管经理，窗口一年内应有提示；窗口过后跳过）
    _chg_lbl = mgr_change_label(get_client().manager_tenure("005827"))
    if _chg_lbl:
        assert any("近一年基金经理有变更" in w and "005827" in w for w in warn_texts), warn_texts
    assert any("本组经理汇总" in e.label for e in at.expander), [e.label for e in at.expander]


def test_share_class_a_row():
    """多级别报表按查询代码的份额级别取行：010790（A类）经理 A 类 >100；
    从业人员双口径：A类 1.6262%（A类份额为分母）/ 合计 803.86万份(0.4945%)"""
    at = query_groups([("我的基金", "010790")])
    df = overview_dfs(at)[0]
    row = df[df["代码"] == "010790"].iloc[0]
    assert row["基金经理持有本基金"] == ">100万份", f"010790 解析错误: {row['基金经理持有本基金']}"
    assert row["从业人员持有本基金"] == "A类 1.6262% / 合计 803.86万份(0.4945%)", \
        f"010790 从业人员双口径错误: {row['从业人员持有本基金']}"


def test_share_class_c_not_mixed_into_a():
    """多级别报表不得把 C 类持有算到 A 类头上：015887（A类）经理 A 类 0~10、C 类 >100"""
    at = query_groups([("我的基金", "015887")])
    df = overview_dfs(at)[0]
    row = df[df["代码"] == "015887"].iloc[0]
    assert row["基金经理持有本基金"] == "0~10万份", f"015887 解析错误: {row['基金经理持有本基金']}"


def test_two_groups_cross_dup_and_manager_drilldown():
    """两个分组各自出表（切换分组时渲染）、跨组重复提醒；点经理按钮就地展示其全部在管基金"""
    # 010790 跨两组重复；第一组经理含吴昊（海富通，id 30132788）
    at = query_groups([("我的基金", "010790"), ("分组2", "161725 010790")])
    # stateful tabs 只渲染选中分组（默认第一组），切换时整页 rerun 只重画目标组
    dfs = overview_dfs(at)
    assert len(dfs) == 1, f"只应渲染选中的第一组，实际 {len(dfs)}"
    assert list(dfs[0]["代码"]) == ["010790"], dfs[0].to_string()
    warn_texts = [w.value for w in at.warning]
    assert any("010790" in w and "重复" in w for w in warn_texts), warn_texts
    assert sum("本组经理汇总" in e.label for e in at.expander) == 1, \
        [e.label for e in at.expander]  # 选中分组一个经理汇总，另一组未渲染
    gmgr_btns = [b for b in at.button if b.key and b.key.startswith("gmgr_")]
    assert gmgr_btns, f"应渲染经理下钻按钮，实际 {[b.key for b in at.button]}"
    wh = next((b for b in gmgr_btns if b.key == "gmgr_我的基金_30132788"), None)  # 吴昊（海富通）
    assert wh is not None, f"010790 的经理吴昊应在下钻按钮中，实际 {[b.key for b in gmgr_btns]}"
    wh.click().run()
    assert any("点击表头排序" in c.value for c in at.caption), [c.value for c in at.caption]
    mv = next(b for b in at.button if b.key == "mvfund_30132788_010790")  # 吴昊在管基金按钮已渲染
    # 分组页点经理视图内的基金：详情就地显示在该经理视图内（页面归属 groups）
    mv.click().run()
    assert any("010790" in s.value for s in at.success), [s.value for s in at.success]
    # 切换到第二组：该组渲染（表+经理汇总），第一组不再渲染，经理视图不跨组遗留
    switch_group(at, "分组2（2只）")
    dfs = overview_dfs(at)
    assert len(dfs) == 1 and list(dfs[0]["代码"]) == ["161725", "010790"], dfs[0].to_string()
    assert sum("本组经理汇总" in e.label for e in at.expander) == 1, [e.label for e in at.expander]
    assert not any(b.key and b.key.startswith("mvfund_") for b in at.button), \
        "第一组的经理视图（下钻）不应出现在第二组"


def test_groups_persist_across_sessions():
    """输入自动保存：全新会话（模拟下次启动）免输入直接查询，切换分组均可查"""
    at = query_groups([("我的基金", "005827"), ("分组2", "161725")])
    at2 = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at2.run()
    assert len(at2.text_area) == 2, "分组数应从 我的基金.json 恢复"
    assert "161725" in at2.text_area[1].value, at2.text_area[1].value
    click_query(at2)
    dfs = overview_dfs(at2)
    assert len(dfs) == 1 and list(dfs[0]["代码"]) == ["005827"], "恢复后直接查出选中分组结果"
    switch_group(at2, "分组2（1只）")
    dfs = overview_dfs(at2)
    assert len(dfs) == 1 and list(dfs[0]["代码"]) == ["161725"], dfs[0].to_string()


def test_group_reorder_persists():
    """「↓/↑」调整分组显示顺序并持久化"""
    at = query_groups([("我的基金", "005827"), ("分组2", "161725")])
    with open(DATA_FILE, encoding="utf-8") as f:
        gids = [g["id"] for g in json.load(f)["groups"]]
    assert len(gids) == 2, gids
    down_btn = next(b for b in at.button if b.key == f"down_{gids[0]}")
    down_btn.click().run()
    assert "161725" in at.text_area[0].value, at.text_area[0].value
    assert "005827" in at.text_area[1].value, at.text_area[1].value
    at3 = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at3.run()
    assert "161725" in at3.text_area[0].value, "排序应持久化到 我的基金.json"


def test_requery_clears_previous_results_before_query(monkeypatch):
    """再次点「开始查询」为两段式：点击帧先清空上一轮结果并正常收尾，
    第二帧（iframe 自动回点/再点一次）才执行查询。查询入口时刻会话里不得残留
    旧结果（否则长查询期间旧表以灰色 stale 状态滞留整页）；
    查询完成后 pending 标记必须已消费，结果正常重渲染。"""
    import streamlit as st

    import ui.query as query_mod
    from ui.state import KEY_QUERY_PENDING, KEY_RESULTS

    fake_row = {"代码": "005827", "名称": "易方达蓝筹精选混合", "类型": "混合型", "基金经理": "张坤",
                "规模日期": "--", "净值日期": "--", "nav_yuan": None, "small": False,
                "r1m": None, "r3m": None, "r6m": None, "r1y": None, "r2y": None,
                "rytd": None, "r3y": None, "r5y": None, "r1y_peer_pct": None,
                "r3y_peer_pct": None, "r5y_peer_pct": None, "holding": "", "holding_src": "--",
                "holding_chg": "", "mgr_chg": ""}
    captured = []  # 每次查询入口时刻的 (会话旧结果, 代码列表)

    def fake_query_all(codes, with_holding, status_box, progress_bar):
        captured.append((st.session_state.get(KEY_RESULTS), list(codes)))
        return ({c: dict(fake_row, 代码=c) for c in codes}, {})

    monkeypatch.setattr(query_mod, "query_all", fake_query_all)
    at = query_groups([("我的基金", "005827")])
    dfs = overview_dfs(at)
    assert dfs and list(dfs[0]["代码"]) == ["005827"], "首轮查询应出表"
    assert KEY_QUERY_PENDING not in at.session_state, "首轮完成后 pending 不应残留"
    assert len(captured) == 1, captured

    click_query(at)  # 第二次点开始查询（两段式）
    dfs = overview_dfs(at)
    assert dfs and list(dfs[0]["代码"]) == ["005827"], "再次查询后结果应重渲染"
    assert KEY_QUERY_PENDING not in at.session_state, "查询完成后 pending 应已消费"
    assert len(captured) == 2, captured
    assert captured[1][0] is None, "第二次查询入口时刻，上一轮结果应已被点击帧清空"
    assert captured[1][1] == ["005827"], captured


# ---------------- 单只 / 经理查询页 ----------------
def _run_single(kw):
    at = AppTest.from_file(SINGLE_PAGE, default_timeout=180)
    at.run()
    next(t for t in at.text_input if t.key == "single_q").set_value(kw).run()
    next(b for b in at.button if b.key == "single_go").click().run()
    return at


def test_single_code_query_and_history():
    """按代码单只查询，详情含经理持有，写入历史"""
    at = _run_single("005827")
    assert any("005827" in s.value for s in at.success), [s.value for s in at.success]
    assert "基金经理持有份额（区间）" in [m.label for m in at.metric], [m.label for m in at.metric]
    with open(HISTORY_FILE, encoding="utf-8") as f:
        hist = json.load(f)["items"]
    assert hist and hist[0]["code"] == "005827", hist


def test_single_name_search():
    """按名称搜索，选择后查看"""
    at = _run_single("蓝筹精选")
    assert len(at.selectbox) == 1, "名称搜索应出现基金选择框"
    sb = at.selectbox[0]
    assert sb.options, "搜索结果不应为空"
    pick_code = sb.value  # .value 是 6 位代码；.options 是格式化标签
    next(b for b in at.button if b.key == "single_view").click().run()
    assert pick_code in " ".join(s.value for s in at.success)


def test_history_chip_cross_session():
    """历史记录跨会话保留，点击可直接再查"""
    _run_single("005827")
    at5 = AppTest.from_file(SINGLE_PAGE, default_timeout=180)
    at5.run()
    chip = next((b for b in at5.button if b.key == "hist_005827"), None)
    assert chip is not None, "新会话应显示历史记录按钮"
    chip.click().run()
    assert any("005827" in s.value for s in at5.success)


def test_search_ranking_prefers_exact_suffix_match():
    """搜索相关度重排：输入含多余后缀时，正确基金应排第一（华商创新成长 000541）"""
    at = _run_single("华商创新成长灵活配置")
    assert at.selectbox, "应出现基金选择框"
    assert at.selectbox[0].value == "000541", f"应为华商创新成长，实际 {at.selectbox[0].value}"


def test_manager_view_sortable_and_fund_detail():
    """经理视图：可点表头排序（含 CSV 导出），点击基金查详情"""
    at = _run_single("张坤")
    mgr_btn = next((b for b in at.button if b.key == "mgr_30189744"), None)
    assert mgr_btn is not None, f"应出现张坤（易方达基金）的经理卡片，实际 {[b.key for b in at.button]}"
    mgr_btn.click().run()
    # 表格渲染为内嵌组件（iframe），AppTest 不可见；用渲染提示与基金按钮确认经理视图已出
    assert any("点击表头排序" in c.value for c in at.caption), [c.value for c in at.caption]
    next(b for b in at.button if b.key == "mvfund_30189744_005827").click().run()
    assert any("易方达蓝筹精选" in s.value for s in at.success), [s.value for s in at.success]


def test_multi_manager_blocks_with_inplace_detail():
    """一次输入多位经理（张坤 杨思亮），分块展示各自在管基金；分块内点基金详情就地显示"""
    at = _run_single("张坤 杨思亮")
    cap = " ".join(c.value for c in at.caption)
    assert "共匹配 2 位基金经理" in cap, cap[:200]
    assert any(b.key == "mvfund_30189744_005827" for b in at.button), "张坤的基金按钮应存在"
    yang_id = get_client().search_managers("杨思亮")[0]["id"]
    assert any(b.key and b.key.startswith(f"mvfund_{yang_id}_") for b in at.button), \
        f"杨思亮的基金按钮应存在，实际 {[b.key for b in at.button if b.key and b.key.startswith('mvfund_')]}"
    # 共管基金（005827 两人都在管）不会因 key 冲突报错，且两位各有一份
    assert sum("点击表头排序" in c.value for c in at.caption) >= 2, "两位经理各应有一个排序表"
    yang_codes = get_client().search_managers("杨思亮")[0]["codes"]
    assert "005827" in yang_codes
    next(b for b in at.button if b.key == f"mvfund_{yang_id}_005827").click().run()
    assert any("易方达蓝筹精选" in s.value for s in at.success), [s.value for s in at.success]


def test_manager_detail_not_leaking_across_pages():
    """多页共用会话：分组页经理视图点开的基金详情不得遗留到单只/经理查询页。
    模拟分组页状态（scope=manager 且 page=groups）后在本页查同一位经理，
    经理视图内与页面底部都不应出现该详情；本页重新点开则正常显示。"""
    at = AppTest.from_file(SINGLE_PAGE, default_timeout=180)
    at.session_state[KEY_DETAIL_SCOPE] = "manager"
    at.session_state[KEY_DETAIL_PAGE] = "groups"
    at.session_state[KEY_DETAIL_MANAGER] = "30189744"
    at.session_state[KEY_SINGLE_CODE] = "005827"
    at.run()
    next(t for t in at.text_input if t.key == "single_q").set_value("张坤").run()
    next(b for b in at.button if b.key == "single_go").click().run()
    next(b for b in at.button if b.key == "mgr_30189744").click().run()
    assert any("点击表头排序" in c.value for c in at.caption), "经理视图应已渲染"
    assert not [s for s in at.success if "蓝筹精选" in s.value], \
        f"分组页点开的详情不得遗留到本页，实际 {[s.value for s in at.success]}"
    # 同一只基金在本页点开，详情正常就地出现（页面归属校验不拦本页自己的交互）
    next(b for b in at.button if b.key == "mvfund_30189744_005827").click().run()
    assert any("易方达蓝筹精选" in s.value for s in at.success), [s.value for s in at.success]


def test_same_name_manager_hints():
    """同名经理提示（目录中 吴昊 6 位、李博 3 位重名），单人查询与多人查询都有提示"""
    at = _run_single("吴昊")
    infos = [i.value for i in at.info]
    assert any("同名基金经理" in v and "吴昊" in v and "6 位" in v for v in infos), infos
    assert sum(1 for b in at.button if b.key and b.key.startswith("mgr_")) == 6, \
        [b.key for b in at.button if b.key and b.key.startswith("mgr_")]
    at2 = _run_single("李博 张坤")
    infos2 = [i.value for i in at2.info]
    assert any("同名基金经理" in v and "李博" in v and "3 位" in v for v in infos2), infos2


# ---------------- 纯函数：原始行/展示行/经理聚合/排序键 ----------------
def test_parse_period_returns():
    """阶段涨幅解析：Y/3Y/6Y/1N/2N/JN/3N/5N -> r1m/r3m/r6m/r1y/r2y/rytd/r3y/r5y；空串（未满期）忽略"""
    datas = [
        {"title": "Z", "syl": "1.23"},          # 近1周：不用
        {"title": "Y", "syl": "-4.07"},
        {"title": "3Y", "syl": "-4.58"},
        {"title": "6Y", "syl": ""},              # 未满 6 月
        {"title": "1N", "syl": "-28.73"},
        {"title": "JN", "syl": "-21.75"},
        {"title": "3N", "syl": "-25.78"},
        {"title": "5N", "syl": ""},              # 未满 5 年
        {"title": "2N", "syl": "-24.58"},
        {"title": "LN", "syl": None},            # 成立来：不用
    ]
    assert parse_period_returns(datas) == {
        "r1m": -4.07, "r3m": -4.58, "r6m": None, "r1y": -28.73,
        "r2y": -24.58, "rytd": -21.75, "r3y": -25.78, "r5y": None,
    }
    empty = {"r1m": None, "r3m": None, "r6m": None, "r1y": None,
             "r2y": None, "rytd": None, "r3y": None, "r5y": None}
    assert parse_period_returns([]) == empty
    assert parse_period_returns(None) == empty


def test_parse_jdzf_ranks():
    """F10 阶段涨幅页解析：各期 '排名|总数' -> 百分位（0=最好 100=最差）；
    近1周/成立来不取，未满期/无排名记 None。样例取自 005827 真实返回的裁剪。"""
    sample = (
        "var apidata={ content:\"<div class='jdzfnew'>"
        "<ul class='fcol'><li class='title'></li><li>涨幅</li><li>同类平均</li><li>同类排名</li></ul>"
        "<ul><li class='title'>今年来</li><li class='tor grn bold'>-21.75%</li>"
        "<li class='tor red bold'>2.35%</li><li class='tlpm'>4682<font class='gray'>|</font>4994</li></ul>"
        "<ul><li class='title'>近1周</li><li class='tlpm'>1796<font class='gray'>|</font>5449</li></ul>"
        "<ul><li class='title'>近1月</li><li class='tlpm'>1868<font class='gray'>|</font>5536</li></ul>"
        "<ul><li class='title'>近3月</li><li class='tlpm'>1751<font class='gray'>|</font>5417</li></ul>"
        "<ul><li class='title'>近6月</li><li class='tlpm'>4893<font class='gray'>|</font>5186</li></ul>"
        "<ul><li class='title'>近1年</li><li class='tor grn bold'>-28.73%</li>"
        "<li class='tor red bold'>0.77%</li><li class='tlpm'>4573<font class='gray'>|</font>4787</li></ul>"
        "<ul><li class='title'>近2年</li><li class='tlpm'>4168<font class='gray'>|</font>4244</li></ul>"
        "<ul><li class='title'>近3年</li><li class='tlpm'>3576<font class='gray'>|</font>3701</li></ul>"
        "<ul><li class='title'>近5年</li><li class='tlpm'>2037<font class='gray'>|</font>2216</li></ul>"
        "<ul><li class='title'>近3月空缺</li><li class='tlpm'>---</li></ul>"
        "<ul class='last'><li class='title'>成立来</li><li class='tor red bold'>45.73%</li>"
        "<li class='tlpm'>---</li></ul>"
        "\"};"
    )
    assert parse_jdzf_ranks(sample) == {
        "rytd": 93.75, "r1m": 33.74, "r3m": 32.32, "r6m": 94.35,
        "r1y": 95.53, "r2y": 98.21, "r3y": 96.62, "r5y": 91.92,
    }
    assert parse_jdzf_ranks("var apidata=") == dict.fromkeys(
        ("r1m", "r3m", "r6m", "rytd", "r1y", "r2y", "r3y", "r5y"))


def test_peer_rank_column_display_and_coloring():
    """同类排名(近1年/近3年)：'前X%'格式（向上取整、最小1）、数值排序键、
    前25%红字/后25%绿字（Styler 与 HTML 表两套渲染路径语义一致）"""
    from ui.render import _RET_DOWN_FG, _RET_UP_FG, _highlight_changes, _styler_col_name, display_sort_values

    base = {"代码": "A", "名称": "测试", "类型": "--", "基金经理": "--", "规模日期": "--",
            "净值日期": "--", "nav_yuan": None, "small": False, "r1m": None, "r3m": None,
            "r6m": None, "r1y": None, "r2y": None, "rytd": None, "r3y": None, "r5y": None,
            "holding": "", "holding_src": "--", "holding_chg": "", "mgr_chg": ""}
    rows = [dict(base, 代码="A", r1y_peer_pct=0.25, r3y_peer_pct=12.0, r5y_peer_pct=50.0),  # 不足1%按 前1%
            dict(base, 代码="B", r1y_peer_pct=12.0, r3y_peer_pct=50.0, r5y_peer_pct=None),
            dict(base, 代码="C", r1y_peer_pct=50.0, r3y_peer_pct=None, r5y_peer_pct=99.9),
            dict(base, 代码="D", r1y_peer_pct=95.53, r3y_peer_pct=99.9, r5y_peer_pct=1.0),
            dict(base, 代码="E", r1y_peer_pct=None)]                      # 未满 1 年 -> --
    disp = [display_row(r) for r in rows]
    assert [d["同类排名(近1年)"] for d in disp] == ["前1%", "前12%", "前50%", "前96%", "--"]
    assert [d["同类排名(近3年)"] for d in disp] == ["前12%", "前50%", "--", "前100%", "--"]
    assert [d["同类排名(近5年)"] for d in disp] == ["前50%", "--", "前100%", "前1%", "--"]
    # 分组总览表（pandas Styler）路径
    for cname in ("同类排名(近1年)", "同类排名(近3年)", "同类排名(近5年)"):
        col = pd.Series([d[cname] for d in disp], name=cname)
        if cname == "同类排名(近1年)":
            assert _highlight_changes(col) == [_RET_UP_FG, _RET_UP_FG, "", _RET_DOWN_FG, ""]
        elif cname == "同类排名(近3年)":
            assert _highlight_changes(col) == [_RET_UP_FG, "", "", _RET_DOWN_FG, ""]
        else:
            assert _highlight_changes(col) == ["", "", _RET_DOWN_FG, _RET_UP_FG, ""]
    # 分组总览表的 MultiIndex 两行表头：组名跨列、其余列顶层为空，高亮函数按归一列名工作
    from ui.render import grouped_header_df
    gdf = grouped_header_df(pd.DataFrame(disp))
    assert ("同类排名", "近1年") in gdf.columns and ("", "代码") in gdf.columns, list(gdf.columns)
    assert ("同类排名", "近5年") in gdf.columns, "三列排名都应归入同类排名组"
    assert ("", "规模(净资产)") in gdf.columns, "单独的「规模(净资产)」不得被拆成两行"
    assert ("基金经理", "持有本基金") in gdf.columns, "超长表头应拆成两行（TWO_LINE_HEADERS）"
    assert ("从业人员", "持有本基金") in gdf.columns, "从业人员持有列也应拆成两行（TWO_LINE_HEADERS）"
    assert _styler_col_name(gdf[("基金经理", "持有本基金")]) == "基金经理持有本基金", "归一列名应还原平铺名"
    assert _styler_col_name(gdf[("从业人员", "持有本基金")]) == "从业人员持有本基金", "归一列名应还原平铺名"
    assert _highlight_changes(gdf[("同类排名", "近1年")]) == [_RET_UP_FG, _RET_UP_FG, "", _RET_DOWN_FG, ""], \
        "完整元组列名选中（Styler.apply 实际传入的形式）才能命中归一逻辑"
    # 经理视图（HTML 排序表）路径：数值排序键 + 同样的颜色语义
    h = sortable_table_html(pd.DataFrame(disp), sort_values=display_sort_values(rows))
    assert '<td class="ret-up" data-sort="12.0"' in h, h
    assert '<td class="ret-down" data-sort="95.53"' in h, h
    assert '<td class="" data-sort="50.0"' in h, h
    assert '<td class="ret-up" data-sort="12.0"' in h and 'data-sort="99.9"' in h, h
    assert '<td class="" data-sort=""' in h, "未满期不着色且排序沉底"


def test_pct_format():
    """阶段涨幅格式化：两位小数百分数，None/非数字 -> '--'（新基金未满期）"""
    assert pct(-4.07) == "-4.07%"
    assert pct(28) == "28.00%"
    assert pct(None) == "--"
    assert pct("") == "--"
    assert pct("abc") == "--"


def test_fmt_employee_holding():
    """从业人员持有格式化：过万折算万份两位小数、不足万显示原值份；
    占比最多4位小数去尾零；0/None 兜底"""
    assert fmt_employee_holding(8544761.74, 0.0639) == "854.48万份(0.0639%)"
    assert fmt_employee_holding(9038378.91, 8.335325) == "903.84万份(8.3353%)"
    assert fmt_employee_holding(101.63, 0.000174) == "101.63份(0.0002%)"
    assert fmt_employee_holding(297982.82, None) == "29.80万份"
    assert fmt_employee_holding(0, 0.0) == "0（无人持有）"
    assert fmt_employee_holding(None, None) == "--"


def test_fmt_employee_holding_pair():
    """多级别基金双口径：'A类 6.81% / 合计 225.93万份(3.46%)'；
    级别行缺失/单级别退回合计单值格式"""
    from ui.render import fmt_employee_holding_pair
    assert fmt_employee_holding_pair("A", 6.8095, 2259263.94, 3.4635) == \
        "A类 6.8095% / 合计 225.93万份(3.4635%)"
    assert fmt_employee_holding_pair("A", 1.6262, 8038597.09, 0.4945) == \
        "A类 1.6262% / 合计 803.86万份(0.4945%)"
    assert fmt_employee_holding_pair("A", None, 2259263.94, 3.4635) == "225.93万份(3.4635%)"
    assert fmt_employee_holding_pair("", None, None, None) == "--"


def test_highlight_changes_coloring():
    """涨跌着色：持有变化列 ↑绿底↓红底；阶段涨幅列 正红字负绿字（A 股红涨绿跌，文字色）、-- 不着色；其他列不着色"""
    from ui.render import _CHG_DOWN_BG, _CHG_UP_BG, _RET_DOWN_FG, _RET_UP_FG, _highlight_changes
    ret = _highlight_changes(pd.Series(["-4.07%", "1.50%", "--"], name="近1月"))
    assert ret == [_RET_DOWN_FG, _RET_UP_FG, ""], ret
    assert _RET_UP_FG.startswith("color:") and _RET_DOWN_FG.startswith("color:"), "阶段涨幅应为文字色"
    chg = _highlight_changes(pd.Series(["↑2档", "↓1档", "→持平"], name="持有较上期"))
    assert chg == [_CHG_UP_BG, _CHG_DOWN_BG, ""], chg
    assert _highlight_changes(pd.Series(["1.50%", "--"], name="近3月")) == [_RET_UP_FG, ""]
    assert _highlight_changes(pd.Series(["x", "y"], name="名称")) == ["", ""]


def test_fund_overview_row_raw_and_display():
    """原始行保留数值/枚举字段，display_row 负责全部展示格式化"""
    raw = fund_overview_row("005827", with_holding=True)
    assert raw["holding"] == ">100", raw
    assert raw["nav_yuan"] and raw["nav_yuan"] > 1e9, raw["nav_yuan"]
    assert raw["small"] is False
    assert isinstance(raw["r1y"], float), raw  # 老基金各期阶段涨幅都应拿到数值
    assert all(isinstance(raw[k], float) for k in ("r1m", "r3m", "r6m", "r2y", "rytd", "r3y", "r5y")), raw
    assert isinstance(raw["r1y_peer_pct"], float), raw  # 近1年同类排名百分位
    assert isinstance(raw["r3y_peer_pct"], float), raw  # 近3年同类排名百分位
    assert isinstance(raw["r5y_peer_pct"], float), raw  # 近5年同类排名百分位
    assert raw["emp_shares"] == 8544761.74, raw  # 从业人员精确份额（份）
    assert raw["emp_pct"] == 0.0639, raw  # 占基金总份额比例（%）
    assert raw["emp_cls"] == "" and raw["emp_cls_pct"] is None, raw  # 单级别基金无级别行，退回合计口径
    disp = display_row(raw)
    assert disp["基金经理持有本基金"] == ">100万份", disp
    assert disp["从业人员持有本基金"] == "854.48万份(0.0639%)", disp
    assert disp["规模(净资产)"].endswith("亿") and not disp["规模(净资产)"].endswith("亿 ⚠️")
    assert disp["近1年"] == pct(raw["r1y"]) and disp["近1年"].endswith("%"), disp
    assert disp["近2年"] == pct(raw["r2y"]), disp
    assert disp["今年以来"] == pct(raw["rytd"]) and disp["近5年"] == pct(raw["r5y"]), disp
    assert disp["同类排名(近1年)"] == peer_pct(raw["r1y_peer_pct"]), disp
    assert disp["同类排名(近3年)"] == peer_pct(raw["r3y_peer_pct"]), disp
    assert disp["同类排名(近5年)"] == peer_pct(raw["r5y_peer_pct"]), disp
    small_raw = dict(raw, small=True, nav_yuan=0.3e8, holding="0", holding_chg="降2档")
    d2 = display_row(small_raw)
    assert d2["规模(净资产)"].endswith("⚠️") and d2["基金经理持有本基金"] == "0（未持有）"
    assert d2["持有较上期"] == "↓2档"
    _none8 = {k: None for k in ("r1m", "r3m", "r6m", "r1y", "r2y", "rytd", "r3y", "r5y",
                                 "r1y_peer_pct", "r3y_peer_pct", "r5y_peer_pct")}
    d3 = display_row(dict(raw, **_none8))
    assert d3["近1月"] == "--" and d3["近2年"] == "--" and d3["近5年"] == "--", d3  # 未满期/获取失败兜底
    assert d3["同类排名(近1年)"] == "--" and d3["同类排名(近3年)"] == "--" and d3["同类排名(近5年)"] == "--", d3


def test_mgr_change_label_window():
    """经理变更判定：新任/离任/组合/无变更"""
    import datetime as _dt
    _today = _dt.date.today()
    _d = lambda n: str(_today - _dt.timedelta(days=n))  # noqa: E731
    assert mgr_change_label([{"start": _d(30), "end": "至今"}]) == "新任"
    assert mgr_change_label([{"start": _d(400), "end": _d(20)}]) == "离任"
    assert mgr_change_label([{"start": _d(400), "end": "至今"},
                             {"start": _d(500), "end": _d(10)}]) == "离任"
    assert mgr_change_label([{"start": _d(15), "end": "至今"},
                             {"start": _d(500), "end": _d(20)}]) == "新任+离任"
    assert mgr_change_label([{"start": _d(400), "end": "至今"}]) == ""
    assert mgr_change_label([]) == ""


def test_group_manager_agg_same_name_and_fallback():
    """经理聚合：同名经理按现任代码区分、目录未覆盖时按姓名兜底、按（持有数,规模）排序"""
    fake_dir = [
        {"id": "m1", "name": "张三", "company": "甲基金", "codes": ["005827", "110011"],
         "names": [], "days": "3000", "scale": "300.00亿", "best_return": "120.00%"},
        {"id": "m2", "name": "张三", "company": "乙基金", "codes": ["161725"],
         "names": [], "days": "400", "scale": "5.00亿", "best_return": "10.00%"},
        {"id": "m3", "name": "王五", "company": "丙基金", "codes": [],
         "names": [], "days": "--", "scale": "--", "best_return": "--"},
    ]
    results = {"005827": {"名称": "蓝筹精选", "基金经理": "张三"},
               "161725": {"名称": "白酒指数", "基金经理": "张三"},
               "999999": {"名称": "新基金", "基金经理": "王五"}}
    items = group_manager_agg(["005827", "161725", "999999"], results, fake_dir)
    rows = manager_summary_rows(items)
    assert len(rows) == 3, rows
    by_c = {r["公司"]: r for r in rows}
    assert by_c["甲基金"]["本组持有"] == "1只" and by_c["甲基金"]["本组基金"] == "蓝筹精选", by_c["甲基金"]
    assert by_c["乙基金"]["本组基金"] == "白酒指数", by_c["乙基金"]  # 同名「张三」各自成行、基金归属正确
    assert by_c["丙基金"]["本组持有"] == "1只", by_c["丙基金"]  # 目录无该基金代码 → 按姓名兜底
    assert rows[0]["公司"] == "甲基金", rows  # 持有只数并列时按在管总规模降序（300亿 > 5亿）
    # 显式传入索引（render_group 的用法）与内部现建索引结果一致
    items_idx = group_manager_agg(["005827", "161725", "999999"], results, fake_dir,
                                   index=build_mgr_dir_index(fake_dir))
    assert items_idx == items


def test_sortable_table_explicit_sort_keys_and_highlights():
    """可排序表格：显式数值排序键 + 迷你基金/经理变更高亮 + 悬停不覆盖高亮"""
    raw = fund_overview_row("005827", with_holding=True)
    fake_raw = {"代码": "999999", "名称": "测试", "类型": "--", "基金经理": "--", "规模日期": "--",
                "净值日期": "--", "nav_yuan": 0.3e8, "small": True, "holding": "",
                "holding_src": "--", "holding_chg": "降2档", "mgr_chg": "新任",
                "r1m": None, "r3m": 1.5, "r6m": None, "r1y": -28.73}
    from ui.render import display_sort_values
    df = pd.DataFrame([display_row(raw), display_row(fake_raw)])
    h = sortable_table_html(df, small_codes={"999999"}, mgr_change_codes={"005827"},
                            sort_values=display_sort_values([raw, fake_raw]))
    assert f'data-sort="{raw["nav_yuan"]}"' in h, "规模排序键应来自原始 nav_yuan（全精度数值）"
    assert f'data-sort="{0.3e8}"' in h, "迷你基金规模键应为 0.3e8"
    assert 'data-sort="4"' in h, "持有>100万份应为档位键 4"
    assert 'data-sort="-28.73"' in h and 'data-sort="1.5"' in h, "阶段涨幅列应为数值排序键"
    assert '<td class="ret-down" data-sort="-28.73"' in h, "负收益应红字"
    assert '<td class="ret-up" data-sort="1.5"' in h, "正收益应绿字"
    assert "td.ret-up { color: #E53935; }" in h and "td.ret-down { color: #3FB950; }" in h, \
        "阶段涨幅文字色样式（正收益红/负收益绿，含深色主题）应存在"
    assert '<td class="" data-sort=""' in h, "未满期（--）不着色且排序沉底"
    assert 'data-sort="-2"' in h, "降2档应为数值键 -2"
    assert 'td class="small"' in h, "迷你基金规模单元格应标红"
    assert 'td class="mgrchg"' in h, "经理变更的基金经理单元格应标琥珀色"
    assert h.count("<tr>") == 4, "两行表头（同类排名分组）+ 2 数据行"
    # 两行分组表头：组名合并跨列、子项在第二行、其余列贯穿；排序/导出只用叶子列
    assert '<th class="grp" colspan="3">同类排名</th>' in h, h
    assert 'onclick="sortTable(this)">近1年<span class="arr">' in h, "分组列叶子表头只显示子项"
    assert '<th rowspan="2" data-col="代码"' in h and '<th rowspan="2" data-col="规模(净资产)"' in h, h
    assert 'data-col="同类排名(近1年)"' in h and 'data-col="同类排名(近5年)"' in h, \
        "data-col 保留完整列名，CSV 导出表头不变"
    assert "基金经理<br>持有本基金<span class=\"arr\">" in h, "超长表头应折成两行显示"
    assert 'data-col="规模(净资产)"' in h and "sortTable" in h and "exportCSV" in h
    assert "tr:hover td:not(.small):not(.up):not(.down):not(.mgrchg)" in h and "tr:hover td {" not in h, \
        "行悬停底色不得覆盖红/绿/琥珀高亮单元格"


# ---------------- 宏观指标页 ----------------
def test_macro_page_renders():
    """宏观页：股债性价比表 + 利率/汇率/宏观指标卡（真实接口，走缓存）"""
    at = AppTest.from_file(MACRO_PAGE, default_timeout=180).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert any("中国10年期国债" in l for l in labels), labels
    assert any("CPI同比" in l for l in labels), labels
    assert any("M2同比" in l for l in labels), labels
    assert any("股债性价比" in c for c in [df.columns for df in at.dataframe] if len(c)) or \
        any("股债性价比" in s.value for s in at.caption), "应展示股债性价比"
    # 指数点位叠加开关：默认开（双轴图），关闭后图表仍正常
    tgl = next(t for t in at.toggle if "指数点位" in t.label)
    assert tgl.value is True
    tgl.set_value(False).run()
    assert not at.exception
    # 股息率口径：切换后汇总表含股息率性价比列（AppTest 的 columns 是 column_config JSON 串）
    metric = next(r for r in at.radio if "口径" in r.label)
    metric.set_value("股息率").run()
    assert not at.exception
    cols = []
    for df in at.dataframe:
        try:
            cols += list(json.loads(df.columns).keys())
        except (ValueError, AttributeError):
            cols += list(df.columns)
    assert any("股息率性价比" in c for c in cols), cols


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_screen_bucket_and_merge():
    """全市场筛选页：类型桶归类（混合型子类独立可选）+ 同基金多份额级别合并（按公告ID）"""
    from ui.page_screen import TYPE_BUCKETS, bucket_of, merge_class_rows
    assert bucket_of("混合型-偏股") == "混合型-偏股"
    assert bucket_of("混合型-偏债") == "混合型-偏债"
    assert bucket_of("混合型-灵活") == "混合型-灵活"
    assert bucket_of("混合型-绝对收益") == "混合型-绝对收益"
    assert bucket_of("股票型") == "股票型"
    assert bucket_of("指数型-股票") == "指数型-股票"
    assert bucket_of("QDII-混合偏股") == "QDII（股票/混合）"
    assert bucket_of("债券型-混合二级") is None
    # 混合型各子类桶的并集须覆盖全部「混合型-*」细分（fundcode_search 的口径）
    mixed = {"混合型-偏股", "混合型-偏债", "混合型-灵活", "混合型-平衡", "混合型-绝对收益"}
    covered = {t for types in TYPE_BUCKETS.values() for t in types if t.startswith("混合型")}
    assert covered == mixed, covered
    rows = [
        {"code": "000423", "name": "前海开源事件驱动混合A", "type": "混合型-灵活",
         "emp_pct": 23.98, "emp_shares": 1.0, "manager_range": ">100",
         "report": "2026-08-31中报", "art": "AN1"},
        {"code": "000424", "name": "前海开源事件驱动混合C", "type": "混合型-灵活",
         "emp_pct": 20.0, "emp_shares": 1.0, "manager_range": ">100",
         "report": "2026-08-31中报", "art": "AN1"},
        {"code": "000326", "name": "南方中小盘成长股票A", "type": "股票型",
         "emp_pct": 2.04, "emp_shares": 2.0, "manager_range": "",
         "report": "2026-08-31中报", "art": "AN2"},
    ]
    merged = merge_class_rows(rows)
    assert len(merged) == 2, merged
    assert merged[0]["codes"] == "000423 000424", merged[0]  # 高占比级别在前，低级别并入
    assert merged[0]["emp_pct"] == 23.98
    assert merged[1]["codes"] == "000326"
