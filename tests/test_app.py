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
from ui.format import display_row, fmt_fee_pct, fmt_employee_holding, peer_pct, pct  # noqa: E402
from ui.render import manager_summary_rows  # noqa: E402
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


def overview_rows(at, gi=0):
    """分组总览的展示行（AppTest 读不到 st.iframe 里的 HTML 排序表，从会话里的
    KEY_PLAN/KEY_RESULTS 按渲染顺序用 display_row 重建；gi=分组序号，默认选中组）。

    「只渲染选中分组」改由经理汇总 expander 的数量断言（stateful tabs + tab.open）。"""
    from ui.format import display_row
    from ui.state import KEY_PLAN, KEY_RESULTS
    results = at.session_state[KEY_RESULTS] or {}
    codes = at.session_state[KEY_PLAN][gi][1]
    return [display_row(results[c]) for c in codes if c in results]


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
    rows = overview_rows(at)
    assert rows, "总览行未构建"
    assert [r["代码"] for r in rows] == ["005827"], "组内重复应合并为一条"
    row = rows[0]
    assert row["名称"] == "易方达蓝筹精选混合", row["名称"]
    assert row["基金经理持有本基金"] == ">100万份", row["基金经理持有本基金"]
    assert row["从业人员持有本基金"] == "854.48万份(0.0639%)", row["从业人员持有本基金"]
    assert any("999999" in e.value for e in at.error), [e.value for e in at.error]
    assert "持有较上期" in row, row
    assert re.fullmatch(r"(↑\d+档|↓\d+档|→持平|--)", row["持有较上期"]), row["持有较上期"]
    assert any("点击表头排序" in c.value for c in at.caption), \
        f"总览表应带排序说明，实际 {[c.value for c in at.caption]}"
    tables = [t.value.data if hasattr(t.value, "data") else t.value for t in at.table]
    assert any("报告期" in list(v.columns) for v in tables if hasattr(v, "columns")), "详情页应有历史对比表"
    warn_texts = [w.value for w in at.warning]
    assert any("005827" in w and "合并" in w for w in warn_texts), warn_texts
    # 近一年经理变更提示（005827 于 2026-05-23 新增共管经理，窗口一年内应有提示；窗口过后跳过）
    _chg_lbl = mgr_change_label(get_client().manager_tenure("005827"))
    if _chg_lbl:
        assert any("近一年基金经理有变更" in w and "005827" in w for w in warn_texts), warn_texts
    assert any("本组经理汇总" in e.label for e in at.expander), [e.label for e in at.expander]


def test_query_scope_only_selected_group():
    """侧边栏「查询范围」选单个分组：计划里只有该组，另一组不出标签页、不参与查询"""
    from ui.state import KEY_PLAN

    write_groups([{"id": "test0001", "name": "我的基金", "codes": "005827"},
                  {"id": "test0002", "name": "分组2", "codes": "161725"}])
    at = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at.run()
    scope_box = next((s for s in at.selectbox if s.label == "查询范围"), None)
    assert scope_box is not None, [s.label for s in at.selectbox]
    assert scope_box.value == "（全部组）", scope_box.value  # 默认行为不变：查全部组
    scope_box.select("分组2").run()
    click_query(at)
    plan = at.session_state[KEY_PLAN]
    assert [n for n, _ in plan] == ["分组2"], plan
    assert plan[0][1] == ["161725"], plan
    assert [r["代码"] for r in overview_rows(at)] == ["161725"]


def test_share_class_a_row():
    """多级别报表按查询代码的份额级别取行：010790（A类）经理 A 类 >100；
    从业人员双口径：A类 1.6262%（A类份额为分母）/ 合计 803.86万份(0.4945%)"""
    rows = overview_rows(query_groups([("我的基金", "010790")]))
    row = next(r for r in rows if r["代码"] == "010790")
    assert row["基金经理持有本基金"] == ">100万份", f"010790 解析错误: {row['基金经理持有本基金']}"
    assert row["从业人员持有本基金"] == "A类 1.6262% / 合计 803.86万份(0.4945%)", \
        f"010790 从业人员双口径错误: {row['从业人员持有本基金']}"


def test_share_class_c_not_mixed_into_a():
    """多级别报表不得把 C 类持有算到 A 类头上：015887（A类）经理 A 类 0~10、C 类 >100"""
    rows = overview_rows(query_groups([("我的基金", "015887")]))
    row = next(r for r in rows if r["代码"] == "015887")
    assert row["基金经理持有本基金"] == "0~10万份", f"015887 解析错误: {row['基金经理持有本基金']}"


def test_two_groups_cross_dup_and_manager_drilldown():
    """两个分组各自出表（切换分组时渲染）、跨组重复提醒；点经理按钮就地展示其全部在管基金"""
    # 010790 跨两组重复；第一组经理含吴昊（海富通，id 30132788）
    at = query_groups([("我的基金", "010790"), ("分组2", "161725 010790")])
    # stateful tabs 只渲染选中分组（默认第一组）：经理汇总 expander 只此一组有
    rows = overview_rows(at)
    assert [r["代码"] for r in rows] == ["010790"], [r["代码"] for r in rows]
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
    rows = overview_rows(at, 1)
    assert [r["代码"] for r in rows] == ["161725", "010790"], [r["代码"] for r in rows]
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
    rows = overview_rows(at2)
    assert [r["代码"] for r in rows] == ["005827"], "恢复后直接查出选中分组结果"
    switch_group(at2, "分组2（1只）")
    rows = overview_rows(at2, 1)
    assert [r["代码"] for r in rows] == ["161725"], [r["代码"] for r in rows]


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


def test_query_abort_shows_banner(monkeypatch):
    """被熔断中止时页面要明确告知"没查完"，且不能误报"所有代码都查询失败"""
    import ui.query as query_mod

    def fake_query_all(codes, with_holding, status_box, progress_bar, benchmarks=None):
        return ({}, {query_mod.QUERY_ABORT_KEY: "数据源连续 3 个请求失败（疑似限流），已暂停 90 秒"})

    monkeypatch.setattr(query_mod, "query_all", fake_query_all)
    at = query_groups([("我的基金", "005827")])
    errs = [e.value for e in at.error]
    assert any("被中止" in e and "疑似限流" in e for e in errs), errs
    assert not any("所有代码都查询失败" in e for e in errs), errs


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

    def fake_query_all(codes, with_holding, status_box, progress_bar, benchmarks=None):
        captured.append((st.session_state.get(KEY_RESULTS), list(codes)))
        return ({c: dict(fake_row, 代码=c) for c in codes}, {})

    monkeypatch.setattr(query_mod, "query_all", fake_query_all)
    at = query_groups([("我的基金", "005827")])
    rows = overview_rows(at)
    assert rows and [r["代码"] for r in rows] == ["005827"], "首轮查询应出表"
    assert KEY_QUERY_PENDING not in at.session_state, "首轮完成后 pending 不应残留"
    assert len(captured) == 1, captured

    click_query(at)  # 第二次点开始查询（两段式）
    rows = overview_rows(at)
    assert rows and [r["代码"] for r in rows] == ["005827"], "再次查询后结果应重渲染"
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
    前25%红字/后25%绿字（总览表统一走 HTML 排序表渲染）"""
    from ui.format import display_sort_values

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
    # 总览表（HTML 排序表）路径：数值排序键 + 前25%红字/后25%绿字 + 两行分组表头
    h = sortable_table_html(pd.DataFrame(disp), sort_values=display_sort_values(rows))
    assert '<td class="ret-up" data-sort="12.0"' in h, h
    assert '<td class="ret-down" data-sort="95.53"' in h, h
    assert '<td class="" data-sort="50.0"' in h, h
    assert '<td class="ret-up" data-sort="12.0"' in h and 'data-sort="99.9"' in h, h
    assert '<td class="" data-sort=""' in h, "未满期不着色且排序沉底"
    assert '<th class="grp" colspan="3">同类排名</th>' in h, "三列排名应合并两行分组表头"
    assert "基金经理<br>持有本基金" in h, "超长表头应折成两行显示（TWO_LINE_HEADERS）"


def test_parse_fee_page():
    """F10 费率页解析：常规横排（007016）与浮动费率竖排（166020）两种版式的真实页面快照"""
    from fundtool.client import parse_fee_page
    t = open(os.path.join(ROOT, "tests", "fixtures", "fees", "jjfl_007016.html"), encoding="utf-8").read()
    fee = parse_fee_page(t)
    assert fee["mng_text"] == "1.20%（每年）" and fee["mng"] == 1.2, fee
    assert fee["cust_text"] == "0.20%（每年）" and fee["cust"] == 0.2, fee
    assert fee["sales_text"] == "---" and fee["sales"] is None, fee  # A 类无销售服务费
    assert fee["total"] == 1.4 and fee["floating"] is False, fee
    assert fee["buy_orig"] == "1.50%" and fee["buy_disc"] == "0.15%", fee  # 申购首档原价/天天折扣
    assert fee["redeem"][0] == ["小于7天", "1.50%"], fee
    assert fee["redeem"][-1] == ["大于等于730天", "0.00%"], fee
    t2 = open(os.path.join(ROOT, "tests", "fixtures", "fees", "jjfl_166020.html"), encoding="utf-8").read()
    f2 = parse_fee_page(t2)
    assert f2["mng"] == 1.6 and f2["floating"] is True, f2  # 业绩挂钩浮动管理费取上限档
    assert "1.60%" in f2["mng_text"] and "0.80%" in f2["mng_text"], f2  # 原文保留各档条款
    assert f2["cust"] == 0.2 and f2["sales"] is None and f2["total"] == 1.8, f2
    empty = parse_fee_page("<html><body>无费率表</body></html>")
    assert empty["total"] is None and empty["redeem"] == [] and empty["buy_orig"] == "", empty


def test_fmt_fee_pct_and_buy_fee():
    """年费用率/申购费格式化：两位小数、浮费上限档带 *、None -> '--'；申购费带天天折扣档"""
    from ui.format import fmt_buy_fee, fmt_fee_pct
    assert fmt_fee_pct(1.4) == "1.40" and fmt_fee_pct(0.6) == "0.60"
    assert fmt_fee_pct(1.8, True) == "1.80*"
    assert fmt_fee_pct(None) == "--"
    assert fmt_buy_fee({"buy_orig": "1.50%", "buy_disc": "0.15%"}) == "1.50% → 天天基金 0.15%"
    assert fmt_buy_fee({"buy_orig": "0.00%", "buy_disc": ""}) == "0.00%"  # C 类等无折扣档
    assert fmt_buy_fee({}) == "--"


def test_sortable_table_fee_highlight():
    """年费用率列：偏高（>1.5%/年）琥珀底提示（按数值排序键判定，浮费带 * 不影响）、-- 不着色"""
    from ui.format import display_row, display_sort_values
    base = {"代码": "007016", "名称": "测试A", "类型": "--", "基金经理": "--", "规模日期": "--",
            "净值日期": "--", "nav_yuan": None, "small": False, "holding": "",
            "fee_total": 1.4, "fee_floating": False}
    high = dict(base, 代码="000002", 名称="测试C", fee_total=1.8, fee_floating=True)
    miss = dict(base, 代码="000003", 名称="测试D", fee_total=None)
    rows = [base, high, miss]
    h = sortable_table_html(pd.DataFrame([display_row(r) for r in rows]),
                            sort_values=display_sort_values(rows))
    assert '<td class="feehigh" data-sort="1.8">1.80*</td>' in h, h
    assert '<td class="" data-sort="1.4">1.40</td>' in h, "常规费率（1.4）不标提示色"
    assert '<td class="" data-sort="">--</td>' in h, "未披露不着色且排序沉底"
    assert "td.feehigh" in h and ":not(.feehigh)" in h, "提示色样式与悬停排除规则应存在"


def test_sortable_table_pe_extreme_highlight():
    """含微利股的基金：两个 PE 单元格琥珀底提示（数字忠实计入、只提醒被放大）；
    亏损股剔除以「剔N」后缀标注（展示文本带后缀、排序键仍为数值）；不含的不标"""
    from ui.format import display_row, display_sort_values
    base = {"代码": "002095", "名称": "电气基金", "类型": "--", "基金经理": "--", "规模日期": "--",
            "净值日期": "--", "nav_yuan": None, "small": False, "holding": "",
            "fee_total": None, "fee_floating": False,
            "pe_w": 30658.91, "pe_avg": 23849.26, "pe_extreme": True, "pe_loss_n": 1}
    clean = dict(base, 代码="007016", 名称="无微利", pe_w=32.75, pe_avg=28.87,
                 pe_extreme=False, pe_loss_n=0)
    rows = [base, clean]
    h = sortable_table_html(pd.DataFrame([display_row(r) for r in rows]),
                            sort_values=display_sort_values(rows),
                            pe_extreme_codes={"002095"})
    assert '<td class="feehigh" data-sort="30658.91">30658.9剔1</td>' in h, h
    assert '<td class="feehigh" data-sort="23849.26">23849.3剔1</td>' in h, h
    assert '<td class="" data-sort="32.75">32.8</td>' in h, "无微利/无亏损的 PE 不带提示色与后缀"


def test_calc_pe_stats():
    """重仓股 PE 统计：按占净值比例加权、盈利股算术平均、亏损/无PE 剔除并计数、覆盖度合计"""
    from fundtool.client import calc_pe_stats
    stocks = [
        {"code": "688401", "name": "路维光电", "pct": 8.87, "pe": 38.19},
        {"code": "300604", "name": "长川科技", "pct": 7.79, "pe": 70.63},
        {"code": "600519", "name": "贵州茅台", "pct": 4.66, "pe": 20.45},
    ]
    st = calc_pe_stats(stocks)
    assert st["weighted"] == 46.17, st  # (8.87*38.19+7.79*70.63+4.66*20.45)/21.32，高权重高PE拉高加权值
    assert st["average"] == 43.09 and st["loss_n"] == 0 and st["valid_n"] == 3, st
    assert st["covered_pct"] == 21.32, st
    loss = calc_pe_stats(stocks + [{"code": "688180", "name": "君实生物-U", "pct": 2.0, "pe": -70.78},
                                   {"code": "899999", "name": "北交所股", "pct": 1.0, "pe": None}])
    assert loss["loss_n"] == 2 and loss["valid_n"] == 3, loss  # 亏损/无PE 不参与计算
    assert loss["weighted"] == st["weighted"] and loss["covered_pct"] == 24.32, loss
    # 微利股计入统计（002095 实况：茂莱光学 11.7 万倍 PE，extreme_n 计数供界面琥珀底提示）：
    # 加权/平均忠实包含茂莱光学与新相微（PE>500 记 extreme_n=2），亏损的汇成真空仍剔除
    real = calc_pe_stats([
        {"code": "688502", "name": "茂莱光学", "pct": 9.7, "pe": 117408.81},
        {"code": "688120", "name": "华海清科", "pct": 9.5, "pe": 98.08},
        {"code": "301392", "name": "汇成真空", "pct": 8.53, "pe": -2578.86},
        {"code": "300260", "name": "新莱应材", "pct": 6.7, "pe": 115.23},
        {"code": "688593", "name": "新相微", "pct": 6.63, "pe": 1376.07},
        {"code": "002851", "name": "麦格米特", "pct": 5.01, "pe": 248.09},
    ])
    assert real["extreme_n"] == 2 and real["loss_n"] == 1 and real["valid_n"] == 5, real
    assert real["average"] == 23849.26, real  # 含 11.7 万倍：数字被显著放大（界面琥珀底提示）
    assert real["weighted"] == 30658.91, real
    assert real["covered_pct"] == 46.07, real  # 覆盖度含全部重仓（口径透明）
    assert calc_pe_stats([])["weighted"] is None, "空持仓"
    assert calc_pe_stats([{"code": "x", "name": "全亏", "pct": 5.0, "pe": -1.0}])["average"] is None, \
        "全亏损无平均"


def test_fmt_pe():
    """PE 格式化：一位小数；亏损/无PE 剔除时追加「剔N」后缀；None/非数字 -> '--'（债基无重仓股/获取失败）"""
    from ui.format import fmt_pe
    assert fmt_pe(27.53) == "27.5" and fmt_pe(20) == "20.0"
    assert fmt_pe(32.75, 2) == "32.8剔2" and fmt_pe(28.87, 1) == "28.9剔1"
    assert fmt_pe(None) == "--" and fmt_pe(None, 3) == "--"  # 无有效股时后缀无意义
    assert fmt_pe("") == "--" and fmt_pe("abc") == "--"


def test_parent_fund_key_and_fof_aggregate():
    """FOF 持仓聚合：母基金键剥离末尾份额字母（ETF/LOF/FOF 的 F/L/T 不误剥）；
    同一 FOF 持有 A/C 双份额只算 1 个持有人；代表份额取被持次数最多档"""
    from fundtool.client import aggregate_fof_holdings, parent_fund_key
    assert parent_fund_key("南方致远混合C") == "南方致远混合"
    assert parent_fund_key("南方致远混合A") == "南方致远混合"
    assert parent_fund_key("华夏沪深300ETF") == "华夏沪深300ETF", "ETF 的 T/F 不得剥"
    assert parent_fund_key("某某混合") == "某某混合"
    rows = [
        {"code": "F00001", "name": "甲FOF", "type": "FOF-进取型", "holdings": [
            {"code": "007416", "name": "南方致远混合C", "pct": 4.76},
            {"code": "007415", "name": "南方致远混合A", "pct": 2.94},
            {"code": "014910", "name": "东方红短债债券A", "pct": 11.17},
        ]},
        {"code": "F00002", "name": "乙FOF", "type": "FOF-稳健型", "holdings": [
            {"code": "007415", "name": "南方致远混合A", "pct": 1.5},
        ]},
    ]
    out = aggregate_fof_holdings(rows)
    assert out[0]["key"] == "南方致远混合" and len(out[0]["holders"]) == 2, out
    assert out[0]["rep_code"] == "007415", "A 被持 2 次应做代表份额"
    top_holder = out[0]["holders"][0]
    assert top_holder["pct"] == 4.76 and len(top_holder["shares"]) == 2, top_holder  # F1 双份额取最高档
    assert out[1]["key"] == "东方红短债债券" and len(out[1]["holders"]) == 1, out


def test_dedupe_fof_universe():
    """全市场 FOF 母基金去重：A/C 归组取代码最小的代表，不同基金不合并"""
    from ui.page_fof import dedupe_fof_universe
    uni = [{"code": "005157", "name": "嘉实领航资产配置混合C", "type": "FOF-稳健型"},
           {"code": "005156", "name": "嘉实领航资产配置混合A", "type": "FOF-稳健型"},
           {"code": "005215", "name": "南方全天候策略A", "type": "FOF-稳健型"}]
    out = dedupe_fof_universe(uni)
    assert len(out) == 2, out
    assert out[0] == {"code": "005156", "name": "嘉实领航资产配置混合A", "type": "FOF-稳健型"}, out


def test_fmt_excess_and_display_bench():
    """超额收益：带符号一位小数格式化；display_row 传基准键时表尾追加 3 列、排序键同步；
    未传基准或行内无该基准 excess 时不加列/显示 --"""
    from ui.format import display_row, display_sort_values, fmt_excess
    assert fmt_excess(4.25) == "+4.2" and fmt_excess(-3.06) == "-3.1" and fmt_excess(0.0) == "+0.0"
    assert fmt_excess(None) == "--" and fmt_excess("") == "--"
    base = {"代码": "005827", "名称": "易方达蓝筹", "类型": "--", "基金经理": "--", "规模日期": "--",
            "净值日期": "--", "nav_yuan": None, "small": False, "holding": ""}
    plain = display_row(dict(base))
    assert "超额-今年以来" not in plain, "不传基准不加超额列"
    row = dict(base, excess={"hs300": {"rytd": 4.25, "r1y": -3.06, "r3y": 12.8}})
    d = display_row(row, "hs300")
    assert d["沪深300超额(今年以来)"] == "+4.2", d
    assert d["沪深300超额(近1年)"] == "-3.1" and d["沪深300超额(近3年)"] == "+12.8", d
    cols = list(d)
    assert cols.index("沪深300超额(近3年)") + 1 == cols.index("近1月"), "超额列插在阶段涨幅块之前（紧邻持有信息）"
    d_empty = display_row(dict(base), "hs300")
    assert d_empty["沪深300超额(近1年)"] == "--", "行内无 excess 数据时列保留但显示 --（列数稳定）"
    d2 = display_row(dict(base, excess={"pghh": {"r1y": 1.5}}), "pghh")
    assert d2["偏股混合超额(近1年)"] == "+1.5", "切换基准列名跟随基准短名"
    sv = display_sort_values([row, dict(base)], "hs300")
    assert sv["沪深300超额(近1年)"] == [-3.06, None], sv


def test_sortable_table_excess_coloring():
    """总览表超额列：组表头两行（上行=基准组名、下行=期间）；着色正红字负绿字（A 股红=跑赢），-- 不着色"""
    df = pd.DataFrame([{"代码": "005827", "沪深300超额(近1年)": "+4.2", "沪深300超额(近3年)": "-3.1",
                        "沪深300超额(今年以来)": "--"}])
    h = sortable_table_html(df)
    assert '<td class="ret-up" data-sort="+4.2">+4.2</td>' in h, h
    assert '<td class="ret-down" data-sort="-3.1">-3.1</td>' in h, h
    assert '<td class="" data-sort="--">--</td>' in h, h
    # 相邻同组超额列自动两行表头：第一行组名（基准）跨3列、第二行期间子项
    assert '<th class="grp" colspan="3">沪深300超额</th>' in h, h
    assert 'onclick="sortTable(this)">近1年<span class="arr">' in h, "分组叶子表头只显示期间"


def test_index_period_returns():
    """基准指数区间涨幅：自然日回溯口径、今年以来取上年末、不足期 None"""
    from datetime import date, timedelta
    from fundtool.macro import index_period_returns
    rows, d, c = [], date(2024, 1, 1), 1000.0
    while d <= date(2026, 10, 21):
        if d.weekday() < 5:
            rows.append({"date": d.isoformat(), "close": c})
            c *= 1.0005
        d += timedelta(days=1)
    out = index_period_returns(rows)
    assert out["r1y"] is not None and 10 < out["r1y"] < 16, out  # 日0.05%复利一年约13%
    assert out["rytd"] is not None and out["r1m"] is not None, out
    assert index_period_returns(rows[-300:])["r3y"] is None, "序列不足3年"
    assert index_period_returns([])["r1y"] is None, "空序列全 None"


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
    from ui.format import fmt_employee_holding_pair
    assert fmt_employee_holding_pair("A", 6.8095, 2259263.94, 3.4635) == \
        "A类 6.8095% / 合计 225.93万份(3.4635%)"
    assert fmt_employee_holding_pair("A", 1.6262, 8038597.09, 0.4945) == \
        "A类 1.6262% / 合计 803.86万份(0.4945%)"
    assert fmt_employee_holding_pair("A", None, 2259263.94, 3.4635) == "225.93万份(3.4635%)"
    assert fmt_employee_holding_pair("", None, None, None) == "--"


def test_highlight_changes_coloring():
    """涨跌着色：持有变化列 ↑绿底↓红底；阶段涨幅列 正红字负绿字（A 股红涨绿跌，文字色）、-- 不着色；其他列不着色"""
    from ui.detail import _CHG_DOWN_BG, _CHG_UP_BG, _RET_DOWN_FG, _RET_UP_FG, _highlight_changes
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
    assert isinstance(raw["fee_total"], float) and 0.1 <= raw["fee_total"] <= 3.0, raw  # F10 费率页合计（%/年）
    assert raw["fee_floating"] is False, raw  # 005827 非浮动费率
    # 重仓股 PE 依赖 push2 行情快照端点：该端点被服务端拒绝时（TLS 握手后直接断开）
    # 字段为 None，跳过这几条口径断言，其余字段照常校验
    if raw["pe_w"] is None:
        print("⚠️ push2 行情端点不可用：跳过重仓股 PE 相关断言")
    else:
        assert isinstance(raw["pe_w"], float) and 5.0 <= raw["pe_w"] <= 60.0, raw  # 前十大加权 PE-TTM
        assert isinstance(raw["pe_avg"], float) and raw["pe_w"] > 0, raw
        assert raw["pe_extreme"] is False, raw  # 005827 重仓无微利异常股
    # 被FOF持有数：值取决于本地是否跑过 FOF 全市场分析（快照 8 天有效），断言与展示口径一致即可
    _fof = raw.get("fof_held")
    assert _fof is None or (isinstance(_fof, int) and _fof >= 0), raw
    disp = display_row(raw)
    assert disp["被FOF持有"] == ("--" if _fof is None else f"{_fof}只"), disp
    assert disp["年费用率%"] == fmt_fee_pct(raw["fee_total"], raw["fee_floating"]), disp
    _suffix = f"剔{raw['pe_loss_n']}" if raw.get("pe_loss_n") else ""
    if raw["pe_w"] is not None:  # 行情端点不可用时 PE 列为 "--"，跳过这两条
        assert disp["持仓加权PE"] == f"{raw['pe_w']:.1f}{_suffix}", disp
        assert disp["持仓平均PE"] == f"{raw['pe_avg']:.1f}{_suffix}", disp
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
    """可排序表格：显式数值排序键 + 迷你基金/经理变更/重复行高亮 + 悬停不覆盖高亮；
    代码/名称列的字符串排序键（名称为拼音键）写入 data-sort，非数值键按 data-sort 文本比较"""
    raw = fund_overview_row("005827", with_holding=True)
    fake_raw = {"代码": "999999", "名称": "测试", "类型": "--", "基金经理": "--", "规模日期": "--",
                "净值日期": "--", "nav_yuan": 0.3e8, "small": True, "holding": "",
                "fee_total": None, "fee_floating": False, "pe_w": None, "pe_avg": None,
                "fof_held": None,
                "holding_src": "--", "holding_chg": "降2档", "mgr_chg": "新任",
                "r1m": None, "r3m": 1.5, "r6m": None, "r1y": -28.73}
    from ui.format import display_sort_values, pinyin_key
    df = pd.DataFrame([display_row(raw), display_row(fake_raw)])
    sv = display_sort_values([raw, fake_raw])
    h = sortable_table_html(df, small_codes={"999999"}, mgr_change_codes={"005827"},
                            sort_values=sv, dup_codes={"999999"})
    assert f'data-sort="{raw["nav_yuan"]}"' in h, "规模排序键应来自原始 nav_yuan（全精度数值）"
    assert f'data-sort="{0.3e8}"' in h, "迷你基金规模键应为 0.3e8"
    assert 'data-sort="4"' in h, "持有>100万份应为档位键 4"
    assert 'data-sort="-28.73"' in h and 'data-sort="1.5"' in h, "阶段涨幅列应为数值排序键"
    assert '<td class="ret-down dup" data-sort="-28.73"' in h, "负收益应红字（重复行叠加 dup）"
    assert '<td class="ret-up dup" data-sort="1.5"' in h, "正收益应绿字（重复行叠加 dup）"
    assert "td.ret-up { color: #E53935; }" in h and "td.ret-down { color: #3FB950; }" in h, \
        "阶段涨幅文字色样式（正收益红/负收益绿，含深色主题）应存在"
    assert '<td class="dup" data-sort=""' in h, "未满期（--）不着色且排序沉底（重复行只带 dup 底色）"
    assert 'data-sort="-2"' in h, "降2档应为数值键 -2"
    assert 'td class="small dup"' in h, "重复行的迷你基金规模单元格应叠加 small+dup（提示色获胜）"
    assert 'class="dup" data-sort="999999"' in h, h
    assert h.count('dup"') == len(df.columns), "重复行每个单元格都应标 dup"
    assert 'td class="mgrchg"' in h, "经理变更的基金经理单元格应标琥珀色（非重复行不受 dup 影响）"
    assert h.index("td.dup {") < h.index("td.small {"), "dup 底色规则应在 small 之前（同格叠加时提示色获胜）"
    # 代码/名称列的排序键：代码按数字（parseFloat 解析前导零）、名称按拼音字符串
    assert sv["代码"] == ["005827", "999999"] and 'data-sort="005827"' in h, h
    assert sv["名称"] == [pinyin_key("易方达蓝筹精选混合"), "ce shi"], sv["名称"]
    assert f'data-sort="{pinyin_key("易方达蓝筹精选混合")}"' in h, "名称列应写拼音排序键"
    # 非数值键的 JS 回退须比较 data-sort（拼音键才能生效），而不是单元格显示文本
    assert "const sa = a.cells[idx].dataset.sort" in h and "localeCompare(sb, 'zh')" in h, h
    assert "tr:hover td:not(.small):not(.up):not(.down):not(.mgrchg):not(.dup)" in h, \
        "行悬停底色不得覆盖红/绿/琥珀高亮单元格（含 dup）"
    assert h.count("<tr>") == 4, "两行表头（同类排名分组）+ 2 数据行"
    # 两行分组表头：组名合并跨列、子项在第二行、其余列贯穿；排序/导出只用叶子列
    assert '<th class="grp" colspan="3">同类排名</th>' in h, h
    assert 'onclick="sortTable(this)">近1年<span class="arr">' in h, "分组列叶子表头只显示子项"
    assert '<th rowspan="2" data-col="代码"' in h and '<th rowspan="2" data-col="规模(净资产)"' in h, h
    assert '<th rowspan="2" data-col="年费用率%"' in h and f'data-sort="{raw["fee_total"]}"' in h, \
        "年费用率列应有表头与数值排序键"
    assert '<th rowspan="2" data-col="持仓加权PE"' in h, "持仓加权PE列应有表头"
    if raw["pe_w"] is not None:  # 行情端点不可用时该列为空，没有数值排序键
        assert f'data-sort="{raw["pe_w"]}"' in h, "持仓加权PE列应有数值排序键"
    assert 'data-col="同类排名(近1年)"' in h and 'data-col="同类排名(近5年)"' in h, \
        "data-col 保留完整列名，CSV 导出表头不变"
    assert "基金经理<br>持有本基金<span class=\"arr\">" in h, "超长表头应折成两行显示"
    assert 'data-col="规模(净资产)"' in h and "sortTable" in h and "exportCSV" in h


def test_pinyin_key_and_name_sort():
    """名称拼音排序键：音节空格分隔、小写，非汉字片段（英文/数字/括号）原样保留小写；
    代码/名称键进入 display_sort_values（总览表「名称」点表头按拼音、「代码」按数字）"""
    from ui.format import display_sort_values, pinyin_key
    assert pinyin_key("易方达蓝筹精选混合") == "yi fang da lan chou jing xuan hun he"
    assert pinyin_key("招商中证白酒指数(LOF)A").startswith("zhao shang zhong zheng bai jiu zhi shu"), \
        pinyin_key("招商中证白酒指数(LOF)A")
    assert "LOF" not in pinyin_key("招商中证白酒指数(LOF)A"), "英文片段应小写"
    assert pinyin_key(None) == "" and pinyin_key(123) == "123"
    rows = [{"代码": "161725", "名称": "招商中证白酒指数(LOF)A"},
            {"代码": "005827", "名称": "易方达蓝筹精选混合"}]
    sv = display_sort_values(rows)
    assert sv["代码"] == ["161725", "005827"], sv["代码"]
    assert sv["名称"] == [pinyin_key("招商中证白酒指数(LOF)A"), pinyin_key("易方达蓝筹精选混合")]
    # 经理汇总表也按拼音排基金经理/公司
    from ui.render import _summary_sort_values
    items = [{"mgr": {"name": "张坤", "company": "易方达", "codes": [], "scale": "--", "days": "--",
                      "best_return": "--"}, "funds": []},
             {"mgr": {"name": "吴昊", "company": "海富通", "codes": [], "scale": "--", "days": "--",
                      "best_return": "--"}, "funds": []}]
    ssv = _summary_sort_values(items)
    assert ssv["基金经理"] == ["zhang kun", "wu hao"], ssv["基金经理"]
    assert ssv["公司"] == ["yi fang da", "hai fu tong"], ssv["公司"]


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
