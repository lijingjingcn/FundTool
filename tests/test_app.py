# -*- coding: utf-8 -*-
"""端到端冒烟测试（Streamlit AppTest，无需浏览器）：
用法: python tests/test_app.py
覆盖：分组查询、错误代码提示、输入本地持久化（第二次启动免输入直接查询）。
会真实调用数据接口/缓存，首次运行需要联网。

通过 FUNDTOOL_DATA_FILE 把数据文件指向临时目录，绝不触碰用户的 我的基金.json。
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

from streamlit.testing.v1 import AppTest  # noqa: E402
from fundtool.holdings import range_change  # noqa: E402
from ui.common import fund_overview_row  # noqa: E402
from ui.sortable_table import sortable_table_html  # noqa: E402

import pandas as pd  # noqa: E402

# 多页应用：入口只做导航，各页面脚本可独立运行（AppTest 直接跑页面脚本）
GROUPS_PAGE = os.path.join(ROOT, "ui", "page_groups.py")
SINGLE_PAGE = os.path.join(ROOT, "ui", "page_single.py")


def write_groups(groups):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, ensure_ascii=False)


def click_query(at):
    btn = next(b for b in at.button if "开始查询" in b.label)
    btn.click().run()


def overview_dfs(at):
    """只取分组总览表（以“代码”列为特征），排除详情里的任职表等。

    总览表用 st.table 渲染（文字可复制）；带高亮时传入 pandas Styler，
    AppTest 取到的 .value 已是底层 DataFrame。
    """
    out = []
    for d in list(at.dataframe) + list(at.table):
        v = d.value
        if hasattr(v, "data"):  # pandas Styler
            v = v.data
        if hasattr(v, "columns") and "代码" in v.columns:
            out.append(v)
    return out


def main():
    write_groups([{"id": "test0001", "name": "我的基金", "codes": ""}])
    # ---- 场景1：默认单分组，输入并查询；005827 组内重复应提示合并 ----
    at = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at.run()
    assert len(at.text_area) == 1, "默认应有 1 个分组输入框"
    at.text_area[0].set_value("005827 005827 999999").run()
    click_query(at)
    dfs = overview_dfs(at)
    assert dfs, "总览表格未渲染"
    df = dfs[0]
    print(df.to_string())
    assert list(df["代码"]) == ["005827"], "组内重复应合并为一条"
    row = df.iloc[0]
    assert row["名称"] == "易方达蓝筹精选混合", row["名称"]
    assert row["基金经理持有本基金"] == ">100万份", row["基金经理持有本基金"]
    assert any("999999" in e.value for e in at.error), [e.value for e in at.error]
    # 持有较上期列 + 详情页历史表（当前中报 vs 上一份年报）
    assert "持有较上期" in df.columns, df.columns
    assert re.fullmatch(r"(↑\d+档|↓\d+档|→持平|--)", row["持有较上期"]), row["持有较上期"]
    tables = [t.value.data if hasattr(t.value, "data") else t.value for t in at.table]
    assert any("报告期" in list(v.columns) for v in tables if hasattr(v, "columns")), "详情页应有历史对比表"
    assert range_change(">100", "10-50") == "升2档" and range_change("10-50", ">100") == "降2档" and range_change("10-50", "10-50") == "持平" and range_change(">100", "") is None
    warn_texts = [w.value for w in at.warning]
    assert any("005827" in w and "合并" in w for w in warn_texts), warn_texts
    print("✅ 场景1 通过：单分组查询 + 无效代码报错 + 组内重复提示")

    # ---- 场景1b：多级别（A/C 份额）报表按查询代码的份额级别取行 ----
    # 010790 是 A 类代码：经理 A 类 >100、C 类 0、合计 >100 → 应显示 A 行 >100
    at.text_area[0].set_value("010790").run()
    click_query(at)
    df = overview_dfs(at)[0]
    row = df[df["代码"] == "010790"].iloc[0]
    assert row["基金经理持有本基金"] == ">100万份", f"010790 解析错误: {row['基金经理持有本基金']}"
    print("✅ 场景1b 通过：010790（A类）取 A 级行（>100万份）")

    # ---- 场景1c：多级别报表不得把 C 类持有算到 A 类头上 ----
    # 015887 是 A 类代码：经理 A 类 0~10、C 类 >100、合计 >100 → 应显示 A 行 0~10
    at.text_area[0].set_value("015887").run()
    click_query(at)
    df = overview_dfs(at)[0]
    row = df[df["代码"] == "015887"].iloc[0]
    assert row["基金经理持有本基金"] == "0~10万份", f"015887 解析错误: {row['基金经理持有本基金']}"
    print("✅ 场景1c 通过：015887（A类）取 A 级行（0~10万份），C类持有未误算")
    at.text_area[0].set_value("010790").run()  # 恢复第一组内容，供场景2构造跨组重复

    # ---- 场景2：添加第二个分组，两组分别查询；010790 跨组重复应触发高亮 ----
    add_btn = next(b for b in at.button if "添加分组" in b.label)
    add_btn.click().run()
    assert len(at.text_area) == 2, "添加后应有 2 个分组输入框"
    at.text_area[1].set_value("161725 010790").run()  # 010790 与第一组重复
    click_query(at)
    dfs = overview_dfs(at)
    assert len(dfs) == 2, f"应有 2 个分组总览表，实际 {len(dfs)}"
    df2 = dfs[1]
    assert list(df2["代码"]) == ["161725", "010790"], df2.to_string()
    warn_texts = [w.value for w in at.warning]
    assert any("010790" in w and "重复" in w for w in warn_texts), warn_texts
    print("✅ 场景2 通过：两个分组各自出表，跨组重复有提醒")

    # ---- 场景3：持久化——全新会话（模拟下次启动）免输入直接查询 ----
    at2 = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at2.run()
    assert len(at2.text_area) == 2, "分组数应从 我的基金.json 恢复"
    assert "161725" in at2.text_area[1].value, at2.text_area[1].value
    click_query(at2)
    assert len(overview_dfs(at2)) == 2, "恢复后应直接查出两个分组结果"
    print("✅ 场景3 通过：输入已保存，下次启动免输入直接查询")

    # ---- 场景4：分组排序——「↓/↑」调整显示顺序并持久化 ----
    with open(DATA_FILE, encoding="utf-8") as f:
        gids = [g["id"] for g in json.load(f)["groups"]]
    assert len(gids) == 2, gids
    down_btn = next(b for b in at2.button if b.key == f"down_{gids[0]}")
    down_btn.click().run()
    assert "161725" in at2.text_area[0].value, at2.text_area[0].value
    assert "010790" in at2.text_area[1].value, at2.text_area[1].value
    at3 = AppTest.from_file(GROUPS_PAGE, default_timeout=180)
    at3.run()
    assert "161725" in at3.text_area[0].value, "排序应持久化到 我的基金.json"
    print("✅ 场景4 通过：分组可上移/下移排序，顺序持久化")

    # ---- 场景5：单只查询——代码直达、经理持有展示、历史记录持久化 ----
    at4 = AppTest.from_file(SINGLE_PAGE, default_timeout=180)
    at4.run()
    next(t for t in at4.text_input if t.key == "single_q").set_value("005827").run()
    next(b for b in at4.button if b.key == "single_go").click().run()
    assert any("005827" in s.value for s in at4.success), [s.value for s in at4.success]
    metric_labels = [m.label for m in at4.metric]
    assert "基金经理持有份额（区间）" in metric_labels, metric_labels
    with open(HISTORY_FILE, encoding="utf-8") as f:
        hist = json.load(f)["items"]
    assert hist and hist[0]["code"] == "005827", hist
    print("✅ 场景5a 通过：按代码单只查询，详情含经理持有，写入历史")

    # ---- 场景5b：按名称搜索，选择后查看 ----
    next(t for t in at4.text_input if t.key == "single_q").set_value("蓝筹精选").run()
    next(b for b in at4.button if b.key == "single_go").click().run()
    assert len(at4.selectbox) == 1, "名称搜索应出现基金选择框"
    sb = at4.selectbox[0]
    assert sb.options, "搜索结果不应为空"
    pick_code = sb.value  # .value 是 6 位代码；.options 是格式化标签
    next(b for b in at4.button if b.key == "single_view").click().run()
    assert pick_code in " ".join(s.value for s in at4.success)
    print(f"✅ 场景5b 通过：按名称搜索到 {len(sb.options)} 只，选择 {pick_code} 查看成功")

    # ---- 场景5c：历史记录跨会话保留，点击可直接再查 ----
    at5 = AppTest.from_file(SINGLE_PAGE, default_timeout=180)
    at5.run()
    chip = next((b for b in at5.button if b.key == "hist_005827"), None)
    assert chip is not None, "新会话应显示历史记录按钮"
    chip.click().run()
    assert any("005827" in s.value for s in at5.success)
    print("✅ 场景5c 通过：历史跨会话保留，点击即查")

    # ---- 场景5d：搜索相关度重排——输入含多余后缀时，正确基金应排第一 ----
    # 官方简称"华商创新成长混合发起式A"不含"灵活配置"，接口原始排序会把
    # 中欧创新成长灵活配置排在前面；重排后目标基金应默认选中
    next(t for t in at4.text_input if t.key == "single_q").set_value("华商创新成长灵活配置").run()
    next(b for b in at4.button if b.key == "single_go").click().run()
    assert at4.selectbox, "应出现基金选择框"
    assert at4.selectbox[0].value == "000541", f"应为华商创新成长，实际 {at4.selectbox[0].value}"
    print("✅ 场景5d 通过：搜索重排后华商创新成长（000541）排第一")

    # ---- 场景5e：按基金经理姓名搜索 → 经理视图（可点表头排序）→ 点击基金出详情 ----
    next(t for t in at4.text_input if t.key == "single_q").set_value("张坤").run()
    next(b for b in at4.button if b.key == "single_go").click().run()
    mgr_btn = next((b for b in at4.button if b.key == "mgr_30189744"), None)
    assert mgr_btn is not None, f"应出现张坤（易方达基金）的经理卡片，实际 {[b.key for b in at4.button]}"
    mgr_btn.click().run()
    # 表格渲染为内嵌组件（iframe），AppTest 不可见；用渲染提示与基金按钮确认经理视图已出
    assert any("点击表头排序" in c.value for c in at4.caption), [c.value for c in at4.caption]
    next(b for b in at4.button if b.key == "mvfund_005827")
    next(b for b in at4.button if b.key == "mvfund_005827").click().run()
    assert any("易方达蓝筹精选" in s.value for s in at4.success), [s.value for s in at4.success]

    # ---- 场景5f：可排序表格的语义排序键与高亮（纯函数直测） ----
    row, _small, _chg = fund_overview_row("005827", with_holding=True)
    assert row["基金经理持有本基金"] == ">100万份", row["基金经理持有本基金"]
    fake = {"代码": "999999", "名称": "测试", "类型": "--", "基金经理": "--", "规模(净资产)": "0.30亿 ⚠️",
            "规模日期": "--", "净值日期": "--", "基金经理持有本基金": "--", "持有数据来源": "--", "持有较上期": "↓2档"}
    h = sortable_table_html(pd.DataFrame([row, fake]), small_codes={"999999"})
    assert 'data-sort="204.16"' in h, "规模应转成数值排序键"
    assert 'data-sort="4"' in h, "持有>100万份应为档位键 4"
    assert 'data-sort="-2"' in h, "降2档应为数值键 -2"
    assert 'td class="small"' in h, "迷你基金规模单元格应标红"
    assert h.count("<tr>") == 3, "表头行 + 2 数据行"
    assert 'data-col="规模(净资产)"' in h and "sortTable" in h and "exportCSV" in h
    print("✅ 场景5e/5f 通过：经理视图可点表头排序（含 CSV 导出），语义排序键与高亮正确")

    print("\n全部冒烟测试通过 🎉")


if __name__ == "__main__":
    main()
