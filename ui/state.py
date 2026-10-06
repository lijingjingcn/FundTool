# -*- coding: utf-8 -*-
"""会话状态与本地持久化。

两件事：
1. st.session_state 的键集中定义（KEY_ 前缀常量）。散落在各页面的字符串字面量
   容易拼错、冲突且难以追溯，页面与渲染层一律从这里引用；
2. 本地文件读写：分组（我的基金.json，输入自动保存）与单只查询历史（查询历史.json）。
"""
import json
import os
import time
import uuid

import streamlit as st

from fundtool import EastFundClient, JsonCache
from fundtool.macro import MacroClient
from ui.constants import BASE_DIR, DATA_FILE, HISTORY_FILE, HISTORY_MAX

# ---------------- st.session_state 键（集中定义，别处只引用不另造） ----------------
# 分组查询页
KEY_GROUPS = "groups"              # 侧边栏分组列表 [{id, name, codes}]
KEY_RESULTS = "results"            # 分组查询结果 {代码: 原始行数据}，行结构见 query.fund_overview_row
KEY_ERRORS = "errors"              # 查询失败的代码 {代码: 原因}
KEY_PLAN = "plan"                  # 本次查询的分组结构 [(分组名, [代码])]
KEY_WITH_HOLDING = "with_holding"  # 本次查询是否勾选了经理持有份额
KEY_DUP_CODES = "dup_codes"        # 跨分组重复出现的代码集合
KEY_CODE_GROUPS = "code_groups"    # {代码: [出现的分组名]}
KEY_DUP_WITHIN = "dup_within"      # {分组名: {代码: 输入次数}} 组内重复输入
KEY_MGR_DIR = "mgr_dir"            # 经理目录的会话级快照（多分组共用一次解析结果）
KEY_GROUP_MGR_VIEW = "group_mgr_view"  # 分组页经理下钻 {"group": 分组名, "mgr": 目录行}
KEY_GROUP_TABS = "group_tabs"      # 分组结果 st.tabs 的选中分组（stateful：存后端会话，防止经理查询等
                                    # 长 rerun/组件重建后弹回第一组；需 streamlit>=1.63 的 tabs key/on_change）

# 单只/经理查询页
KEY_SINGLE_CODE = "single_result_code"    # 当前查看详情的基金代码
KEY_DETAIL_SCOPE = "detail_scope"         # 详情显示位置："page"=页面底部 / "manager"=经理视图内就地显示
KEY_DETAIL_MANAGER = "detail_manager_id"  # 就地详情所属经理视图的经理 ID
KEY_SEARCH_MATCHES = "search_matches"     # 名称搜索匹配到的基金列表
KEY_MANAGER_MATCHES = "manager_matches"   # 单关键词：目录匹配到的经理列表
KEY_MANAGER_VIEW = "manager_view"         # 单关键词：卡片点选的经理（单经理视图）
KEY_MANAGER_VIEWS = "manager_views"       # 多关键词：分块展示的经理列表
KEY_MANAGER_MISSING = "manager_missing"   # 未匹配到经理的输入段
KEY_MANAGER_DUP = "manager_dup_names"     # 同名经理提示 {名字: 匹配人数}


@st.cache_resource
def get_client():
    return EastFundClient(JsonCache(os.path.join(BASE_DIR, ".cache")))


@st.cache_resource
def get_macro():
    return MacroClient(JsonCache(os.path.join(BASE_DIR, ".cache")))


# ---------------- 本地持久化：分组 ----------------
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


def new_group(name):
    return {"id": uuid.uuid4().hex[:8], "name": name, "codes": ""}


# ---------------- 本地持久化：单只查询历史 ----------------
def load_history():
    """单只查询历史：[{code,name,ts}]，最新在前"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            items = (json.load(f) or {}).get("items") or []
    except (OSError, ValueError):
        items = []
    return [it for it in items if isinstance(it, dict) and it.get("code")]


def record_history(code, name):
    """记录一次查询：该代码移到最前，去重，截断到 HISTORY_MAX"""
    items = [it for it in load_history() if it.get("code") != code]
    items.insert(0, {"code": code, "name": name or code, "ts": time.strftime("%Y-%m-%d %H:%M")})
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": items[:HISTORY_MAX]}, f, ensure_ascii=False, indent=2)


def clear_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": []}, f, ensure_ascii=False, indent=2)
