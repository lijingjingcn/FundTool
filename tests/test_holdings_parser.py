# -*- coding: utf-8 -*-
"""定期报告解析器单测（离线，不联网）。

- tests/fixtures/*_holders.txt：从真实中报 PDF 提取的“持有人信息”章节文本窗口
  （提取脚本按 parse_manager_holding 的真实窗口构造，见各文件的来源代码注释）；
- 合成行覆盖：未持有的文字版式、'-'占位、目录行干扰、从业人员精确数、区间档位。

用法: python -m pytest tests/test_holdings_parser.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fundtool.holdings import (  # noqa: E402
    _class_map,
    _find_employees_exact,
    _find_manager_line,
    _range_in,
    format_range,
    parse_manager_holding,
    range_change,
    range_rank,
)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def fixture_lines(name):
    return open(os.path.join(FIXTURES, name), encoding="utf-8").read().split("\n")


# ---------------- 真实报告的文本窗口 ----------------
def test_real_010790_multiclass_takes_query_class_row():
    """A 类代码查询：A>100、C=0、合计>100 —— 应取 A 行，不受合计/C 类影响"""
    val, _, cmap = _find_manager_line(fixture_lines("010790_holders.txt"), "A")
    assert val == ">100"
    assert cmap == {"合计": ">100", "A": ">100", "C": "0"}


def test_real_015887_c_class_not_mixed_into_a():
    """A 类 0~10、C 类 >100：经理只在 C 类重仓持有时，A 类不得显示合计/C 的值"""
    val, _, cmap = _find_manager_line(fixture_lines("015887_holders.txt"), "A")
    assert val == "0~10"
    assert cmap == {"合计": ">100", "A": "0~10", "C": ">100"}


def test_real_005827_single_class_value_on_label_line():
    """单级别基金：值与标签同一行，无份额级别映射"""
    val, line, cmap = _find_manager_line(fixture_lines("005827_holders.txt"), None)
    assert val == ">100"
    assert "本基金基金经理持有本开放式基金" in line
    assert cmap is None


def test_real_001791_label_split_by_class_column_and_85_trap():
    """分级表标签被份额级别列打断（本基金基金经理持有 大成…A 50~100 / 本开放式基金 …C 0）：
    仍须命中 8.4 的经理区间行，不得落到 8.5 发起资金表的同名行（基金经理等人员 603,826.10）"""
    lines = fixture_lines("001791_holders.txt")
    val_a, line_a, cmap = _find_manager_line(lines, "A")
    assert val_a == "50~100", (val_a, line_a, cmap)
    assert cmap == {"A": "50~100", "C": "0", "合计": "50~100"}, cmap
    val_all, _, _ = _find_manager_line(lines, None)
    assert val_all == "50~100"  # 无份额类别时取合计
    val_c, _, _ = _find_manager_line(lines, "C")
    assert val_c == "0"


# ---------------- 合成版式 ----------------
def test_label_and_value_same_line():
    lines = ["9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
             "项目 持有基金份额总量的数量区间（万份）",
             "本基金基金经理持有本开放式基金 10-50"]
    val, _, _ = _find_manager_line(lines)
    assert val == "10-50"


def test_prose_not_held_with_folded_line():
    """文字表述版式：'未持有'句子在'未持/有'之间折行，需拼接后判断"""
    lines = ["报告期内本基金基金经理未持", "有本基金。"]
    val, _, _ = _find_manager_line(lines)
    assert val == "0"


def test_no_fallback_to_other_holder_rows():
    """只有「基金经理等人员/高级管理人员」等其他口径的行时不得代替：宁缺勿错返回 None"""
    lines = [
        "8.4 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
        "高级管理人员、基金经理投资和研究部门负责人持有本开放式基金 10-50",
        "基金经理等人员 603,826.10 3.01 - - -",   # 8.5 发起资金表的行
    ]
    assert _find_manager_line(lines) is None


def test_dash_placeholder_means_zero():
    """占位'-'表示未持有，按 0 处理"""
    lines = ["本基金基金经理持有本开放式基金", "-"]
    val, _, _ = _find_manager_line(lines)
    assert val == "0"


def test_class_map_values_pair_with_nearest_mark():
    """多级别表：值与级别字母同行为准，独行值就近配对（PDF 抽行会打乱列顺序）"""
    lines = ["本公司高级管理人员、基金投资和研究部", "门负责人持有本开放式基金",
             "A", ">100", "C", "0", "合计", ">100"]
    cmap = _class_map(lines, 0, len(lines))
    assert cmap == {"A": ">100", "C": "0", "合计": ">100"}


def test_toc_lines_do_not_pollute_class_map():
    """目录行（标题后跟一串点号和页码）不参与取值"""
    lines = ["8.3 从业人员持有本开放式基金……25", "A >100", "合计 50-100"]
    cmap = _class_map(lines, 0, len(lines))
    assert cmap == {"A": ">100", "合计": "50-100"}


def test_range_token_rejects_thousands_separator_prefix():
    """千分位精确数的前缀不得被当成区间值（603,826.10 的 603、10,605,604.05 的 10）"""
    assert _range_in("基金经理等人员 603,826.10 3.01 - - -") != "603"
    assert _range_in("合计 10,605,604.05 52.80") != "10"
    assert _range_in("本基金基金经理持有 大成绝对收益混合发起A 50~100") == "50~100"


def test_employees_exact_number_after_label():
    """占比列的 % 常只在表头、值是裸数字：按位置取第二个数为占比（不带 % 存储）"""
    lines = ["基金管理人所有从业人员持有本基金 297,982.82 0.021504"]
    assert _find_employees_exact(lines) == {"shares": "297,982.82", "pct": "0.021504"}


def test_employees_exact_with_percent_sign():
    lines = ["基金管理人所有从业人员持有本基金 123,456.78 5.5%"]
    assert _find_employees_exact(lines) == {"shares": "123,456.78", "pct": "5.5"}


def test_employees_exact_folds_to_total_line():
    """标签折行时取随后的合计行（精确份额带千分位；占比按位置取，有无 % 均可）"""
    lines = ["基金管理人的从业人", "员持有本开放式基金", "合计 33,998,609.13 10.978363%"]
    assert _find_employees_exact(lines) == {"shares": "33,998,609.13", "pct": "10.978363"}


def test_employees_exact_fully_fragmented_label_takes_total():
    """标签被 PDF 彻底打碎（基金管/理人所/有从业/人员持/有本基金 与级别行交错，
    “从业人员持有”四字都被拆开）：锚定小节标题行取「合计」（真实版式：景顺长城改革机遇）"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "项目 份额级别 持有份额总数（份） 占基金总份额比例（%）",
        "基金管 景顺长城改革机遇混合A类 9,021,579.86 10.506199",
        "理人所",
        "有从业",
        "人员持 景顺长城改革机遇混合C类 16,799.05 0.074446",
        "有本基",
        "金",
        "合计 9,038,378.91 8.335325",
        "9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
        "本公司高级管理人员、 景顺长城改革机遇混合A类 >100",
        "合计 >100",
    ]
    assert _find_employees_exact(lines) == {
        "shares": "9,038,378.91", "pct": "8.335325",
        "classes": {"A": {"shares": "9,021,579.86", "pct": "10.506199"},
                    "C": {"shares": "16,799.05", "pct": "0.074446"}},
    }


def test_employees_exact_single_class_after_heading():
    """单级基金：小节标题后的完整标签行（标题行自身以“的情况”结尾不得取数）"""
    lines = [
        "8.2 期末基金管理人的从业人员持有本基金的情况",
        "项目 持有份额总数（份） 占基金总份额比例",
        "基金管理人所有从业人员持有本基金 8,038,597.09 0.05%",
    ]
    assert _find_employees_exact(lines) == {"shares": "8,038,597.09", "pct": "0.05"}


def test_employees_exact_value_folded_to_next_line():
    """完整标签行的数值折到下一行"""
    lines = [
        "8.2 期末基金管理人的从业人员持有本基金的情况",
        "基金管理人所有从业人员持有本基金",
        "101.63 0.000174",
    ]
    assert _find_employees_exact(lines) == {"shares": "101.63", "pct": "0.000174"}


def test_employees_exact_range_section_total_not_picked():
    """只有 9.3 区间表（窗口从 9.3 标题开始）时：区间档位的「合计 >100」不是精确份额，不得误取"""
    lines = [
        "9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
        "项目 持有基金份额总量的数量区间（万份）",
        "本公司高级管理人员、 0~10",
        "合计 >100",
    ]
    assert _find_employees_exact(lines) is None


def test_employees_exact_label_breaks_before_you_with_value_on_fragment():
    """标签断在「从业人员持|有本基金」之间且数值与残片同行（真实版式：国泰金龙行业精选）"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "项目 持有份额总数（份） 占基金总份额比例",
        "基金管理人所有从业人员持 894,804.86 0.11%",
        "有本基金",
        "9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
    ]
    assert _find_employees_exact(lines) == {"shares": "894,804.86", "pct": "0.11"}


def test_employees_exact_fragment_value_on_own_line():
    """标签残片行不带数值、数值独立成行（真实版式：…从业人员持 / 894,804.86 0.11% / 有本基金）"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "项目 持有份额总数（份） 占基金总份额比例",
        "基金管理人所有从业人员持",
        "894,804.86 0.11%",
        "有本基金",
        "9.3期末基金管理人的从业人员持有本开放式基金份额总量区间的情况",
    ]
    assert _find_employees_exact(lines) == {"shares": "894,804.86", "pct": "0.11"}


def test_employees_exact_label_breaks_inside_congye():
    """标签断在「所有从业人|员持有本基金」之间（真实版式：海富通收益增长）"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "项目 持有份额总数（份） 占基金总份额比例",
        "基金管理人所有从业人",
        "886,488.66 0.1483%",
        "员持有本基金",
        "9.3期末基金管理人的从业人员持有本开放式基金份额总量区间的情况",
    ]
    assert _find_employees_exact(lines) == {"shares": "886,488.66", "pct": "0.1483"}


def test_employees_exact_section_body_wu_means_zero():
    """小节正文仅「无。」：按 0 处理（真实版式：工银新经济混合，9.3 区间表各级别均为 0）"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "无。",
        "9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
        "本基金基金经理持有本开放式基金 0",
    ]
    assert _find_employees_exact(lines) == {"shares": "0.00", "pct": ""}


def test_employees_exact_with_class_rows():
    """分级基金：合计口径 + 各份额级别行（classes）。真实版式：广发逆向策略 2026 中报——
    员工几乎全持 A 类，A 类口径 6.81% 是合计 3.46% 的近两倍（分母不同）"""
    lines = [
        "8.3 期末基金管理人的从业人员持有本基金的情况",
        "项目 份额级别 持有份额总数（份） 占基金总份额比例",
        "广发逆向策略混合A 2,259,169.66 6.8095%",
        "基金管理人所有从业人员持",
        "广发逆向策略混合C 94.28 0.0003%",
        "有本基金",
        "合计 2,259,263.94 3.4635%",
        "8.4期末基金管理人的从业人员持有本开放式基金份额总量区间的情况",
    ]
    assert _find_employees_exact(lines) == {
        "shares": "2,259,263.94", "pct": "3.4635",
        "classes": {
            "A": {"shares": "2,259,169.66", "pct": "6.8095"},
            "C": {"shares": "94.28", "pct": "0.0003"},
        },
    }


def test_employees_class_rows_not_polluted_by_range_section():
    """classes 只收 9.2 精确表：9.3 区间表的级别行（档位值）不得混入（窗口已截止）"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "海富通均衡甄选混合A 7,887,482.53 1.6262%",
        "基金管理人所有从业人",
        "海富通均衡甄选混合C 151,114.56 0.0132%",
        "员持有本基金",
        "合计 8,038,597.09 0.4945%",
        "9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
        "本公司高级管理人员、基 海富通均衡甄选混合A >100",
        "本基金基金经理持有本开 海富通均衡甄选混合C 0",
    ]
    emp = _find_employees_exact(lines)
    assert emp == {
        "shares": "8,038,597.09", "pct": "0.4945",
        "classes": {
            "A": {"shares": "7,887,482.53", "pct": "1.6262"},
            "C": {"shares": "151,114.56", "pct": "0.0132"},
        },
    }, emp


def test_employees_exact_prose_not_held_means_zero():
    """文字版式：「报告期末，基金管理人的从业人员未持有本基金。」按 0 处理"""
    lines = [
        "9.2 期末基金管理人的从业人员持有本基金的情况",
        "报告期末，基金管理人的从业人员未持有本基金。",
        "9.3 期末基金管理人的从业人员持有本开放式基金份额总量区间情况",
        "本基金基金经理持有本开放式基金 0",
    ]
    assert _find_employees_exact(lines) == {"shares": "0.00", "pct": ""}


# ---------------- 区间档位与格式化 ----------------
def test_range_change_and_rank():
    assert range_change(">100", "10-50") == "升2档"
    assert range_change("10-50", ">100") == "降2档"
    assert range_change("10-50", "10-50") == "持平"
    assert range_change(">100", "") is None
    assert range_rank("0~10") == 1 and range_rank(">100") == 4 and range_rank("") is None


def test_format_range():
    assert format_range(">100") == ">100万份"
    assert format_range("0") == "0（未持有）"
    assert format_range("10-50万份") == "10-50万份"
    assert format_range("") == ""


# ---------------- 真实 PDF 的端到端解析（有缓存才跑） ----------------
# (基金代码, 份额级别, 期望区间, 公告ID)：公告 ID 即 .cache/pdfs 下的文件名
_REAL_PDFS = [
    ("010790", "A", ">100", "AN202608311828785398"),
    ("015887", "A", "0~10", "AN202608281828578985"),
    ("005827", None, ">100", "AN202608311828748204"),
    ("001791", "A", "50~100", "AN202608281828589065"),
]


@pytest.mark.parametrize("code,cls,expect,art", _REAL_PDFS)
def test_parse_real_pdf(code, cls, expect, art):
    path = os.path.join(ROOT, ".cache", "pdfs", f"{art}.pdf")
    if not os.path.exists(path):
        pytest.skip(f"本地无报告 PDF 缓存（{art}.pdf），跳过端到端解析")
    parsed = parse_manager_holding(path, cls)
    assert parsed and parsed["manager_range"] == expect, parsed
