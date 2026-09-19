# -*- coding: utf-8 -*-
"""基金信息查询工具 —— Streamlit 多页应用入口

页面（侧边栏导航切换，会话数据互通）：
- 📚 分组查询    —— 按分组批量查询，总览表 + 详情 + CSV 导出
- 🔍 单只/经理查询 —— 代码直达、名称/经理姓名搜索、经理在管基金总览、查询历史

数据来源：天天基金（东方财富）公开接口 + 基金定期报告 PDF，仅供参考。
"""
import streamlit as st

st.set_page_config(page_title="基金信息查询工具", page_icon="📊", layout="wide")

# st.navigation 返回当前选中的页面，必须显式 .run() 才会执行该页脚本
page = st.navigation(
    [
        st.Page("ui/page_groups.py", title="分组查询", icon="📚", default=True),
        st.Page("ui/page_single.py", title="单只 / 经理查询", icon="🔍"),
    ]
)
page.run()
