# -*- coding: utf-8 -*-
"""全局常量：路径、查询节奏、提醒阈值。所有可调参数集中在此，调整行为只看这一个文件。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 测试通过 FUNDTOOL_DATA_FILE / FUNDTOOL_HISTORY_FILE 指向临时文件，避免覆盖真实数据
DATA_FILE = os.environ.get("FUNDTOOL_DATA_FILE") or os.path.join(BASE_DIR, "我的基金.json")
HISTORY_FILE = os.environ.get("FUNDTOOL_HISTORY_FILE") or os.path.join(BASE_DIR, "查询历史.json")
HISTORY_MAX = 30  # 查询历史最多保留条数

# 批量查询节奏：不设总量上限，全部代码自动分批处理，批次之间稍作停顿以免请求过密。
# 首次查询每只需下载解析报告PDF（约2~5秒/只），已查过的走缓存。
BATCH_SIZE = 30
BATCH_PAUSE = 8  # 批间停顿秒数

# 迷你基金阈值（元）：净资产低于 5000 万的基金有清盘风险，界面特殊提醒
SMALL_NAV_YUAN = 0.5e8
# 经理持有份额对比的报告期数：2 = 当前期报 + 上一份中报/年报
HISTORY_N = 2
# 经理变更提示窗口（天）：近一年内有新任/离任时，基金经理单元格琥珀色提示
MGR_CHANGE_DAYS = 365

# 两行表头的列（平铺列名 -> 上行/下行文字）：表头太长的列拆两行省横向空间。
# 仅影响展示（HTML 表用 <br>、Styler 表用 MultiIndex），CSV 导出与排序键仍用平铺列名
TWO_LINE_HEADERS = {"基金经理持有本基金": ("基金经理", "持有本基金")}

# 股债性价比（ERP）历史分位的解读阈值（%）：≥高位阈值视为股票明显占优，≤低位阈值视为债券明显占优
ERP_HIGH_PCT = 80.0
ERP_LOW_PCT = 20.0
