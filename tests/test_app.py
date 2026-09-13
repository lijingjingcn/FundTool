# -*- coding: utf-8 -*-
"""端到端冒烟测试（Streamlit AppTest，无需浏览器）：
用法: python tests/test_app.py
覆盖：分组查询、错误代码提示、输入本地持久化（第二次启动免输入直接查询）。
会真实调用数据接口/缓存，首次运行需要联网。"""
import io
import json
import os
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from streamlit.testing.v1 import AppTest  # noqa: E402

DATA_FILE = os.path.join(ROOT, "我的基金.json")
BACKUP = DATA_FILE + ".bak"


def setup_backup():
    # 备份用户真实数据，写入受控初始状态，保证测试不依赖用户当前分组
    if os.path.exists(DATA_FILE):
        shutil.move(DATA_FILE, BACKUP)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"groups": [{"id": "test0001", "name": "我的基金", "codes": ""}]}, f, ensure_ascii=False)


def restore_backup():
    if os.path.exists(BACKUP):
        shutil.move(BACKUP, DATA_FILE)
    elif os.path.exists(DATA_FILE):
        os.remove(DATA_FILE)


def click_query(at):
    btn = next(b for b in at.button if "开始查询" in b.label)
    btn.click().run()


def overview_dfs(at):
    """只取分组总览表（以“代码”列为特征），排除详情里的任职表等。

    带高亮的总览表是 pandas Styler，取其 .data 拿到底层 DataFrame。
    """
    out = []
    for d in at.dataframe:
        v = d.value
        if hasattr(v, "data"):  # pandas Styler
            v = v.data
        if hasattr(v, "columns") and "代码" in v.columns:
            out.append(v)
    return out


def main():
    setup_backup()
    try:
        # ---- 场景1：默认单分组，输入并查询；005827 组内重复应提示合并 ----
        at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=180)
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
        warn_texts = [w.value for w in at.warning]
        assert any("005827" in w and "合并" in w for w in warn_texts), warn_texts
        print("✅ 场景1 通过：单分组查询 + 无效代码报错 + 组内重复提示")

        # ---- 场景1b：分级基金（A/C 份额）经理持有取“合计”行，不误取份额级别值 ----
        at.text_area[0].set_value("010790").run()
        click_query(at)
        df = overview_dfs(at)[0]
        row = df[df["代码"] == "010790"].iloc[0]
        assert row["基金经理持有本基金"] == ">100万份", f"010790 解析错误: {row['基金经理持有本基金']}"
        print("✅ 场景1b 通过：010790 分级报表取合计行（>100万份）")

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
        at2 = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=180)
        at2.run()
        assert len(at2.text_area) == 2, "分组数应从 我的基金.json 恢复"
        assert "161725" in at2.text_area[1].value, at2.text_area[1].value
        click_query(at2)
        assert len(overview_dfs(at2)) == 2, "恢复后应直接查出两个分组结果"
        print("✅ 场景3 通过：输入已保存，下次启动免输入直接查询")

        print("\n全部冒烟测试通过 🎉")
    finally:
        restore_backup()


if __name__ == "__main__":
    main()
