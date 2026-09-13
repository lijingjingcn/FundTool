# -*- coding: utf-8 -*-
"""从基金定期报告（年度报告/中期报告）PDF 中提取“基金经理持有本基金”的份额区间。

披露口径（证监会《公开募集证券投资基金信息披露内容与格式准则》）：
- 中报/年报的“基金份额持有人信息”章节披露期末基金管理人从业人员持有本基金的情况；
- “本基金基金经理持有本开放式基金”一行给出区间（万份）：0 / 0-10 / 10-50 / 50-100 / >100；
  部分旧格式报表的行名为“基金经理等人员”；
- 该数据一年最多更新两次（中报 8 月底前、年报次年 3 月底前披露）。
"""
import re

import pdfplumber

from .client import FundApiError

# 章节标题锚点（新/旧格式、中报/年报的措辞差异都覆盖）
_HEADING_PAT = re.compile(
    r"(从业人员持有本(?:开放式)?基金(?:份额总量区间)?的情况"
    r"|高级管理人员、基金经理[^，。]{0,30}区间情况"
    r"|本基金基金经理持有)"
)
# 目录行的特征是标题后跟一串点号和页码
_TOC_LINE = re.compile(r"\.{4,}|…{2,}|\. ?\. ?\. ?\. ?\.")

# 数值/区间：0、10-50、>100、＞100 等
_NUM = r"[<>＞＜]?\s*\d[\d,，]*(?:\.\d+)?(?:\s*[-~～—至]\s*\d[\d,，]*(?:\.\d+)?)?"
# 区间表里的合法值（万份）：必须是独立词块（前后为空白/行尾/括号），
# 小数不超过两位、不含千分位——排除“2026年中期报告”的年份、“§9”的章节号、
# “第 45页”的页码以及 8,038,597.09 这类精确份额数
_NUM_TOKEN = re.compile(
    r"(?:^|[\s（(])([<>＞＜]?\d{1,4}(?:\.\d{1,2})?(?:[-~～—至]\d{1,4}(?:\.\d{1,2})?)?)(?:万?份)?(?:[\s)）、,，]|$)"
)
# 独立的“-”占位符（报告中表示无/零）
_DASH_TOKEN = re.compile(r"(?:^|\s)[-－—](?:\s|$)")


def _norm(val):
    return val.replace("，", ",").replace(" ", "").replace("＞", ">").replace("＜", "<")


def _range_in(text):
    """只提取形如区间表的独立值（0 / 10-50 / >100），排除年份、页码、精确份额数"""
    m = _NUM_TOKEN.search(text)
    return _norm(m.group(1)) if m else None


def _val_in(text):
    """区间值或“-”占位（视为 0）"""
    v = _range_in(text)
    if v:
        return v
    return "0" if _DASH_TOKEN.search(text) else None


def _find_manager_line(lines):
    """定位基金经理持有份额的值。返回 (区间值, 说明行)。

    覆盖的版式：
    - 单级基金：标签和值在同一行，如“本基金基金经理持有本开放式基金 >100”
    - 分级基金（A/C 份额）：标签被折行，各份额级别的值与标签行交错，
      正确取值是随后的“合计”行（如 010790：标签行混入 C 类的 0，合计为 >100）
    - 占位“-”：表示未持有，按 0 处理（如 018554）
    - 旧版式：行名为“基金经理等人员”
    """
    label_idx = None
    for j, line in enumerate(lines):
        if _TOC_LINE.search(line) or "基金经理" not in line:
            continue
        label_idx = j
        break
    if label_idx is None:
        return None
    label = lines[label_idx]
    # 文字表述版式：如“截至本报告期末，……及本基金基金经理未持有本基金。”
    # 注意句子可能在“未持/有”之间折行，需要与下一行拼接后再判断
    follow = lines[label_idx + 1] if label_idx + 1 < len(lines) else ""
    joined = label + follow
    if "未持有" in joined:
        return "0", joined.strip()
    # 分级基金：优先取经理行之后的“合计”行（各份额级别的汇总）
    for line in lines[label_idx + 1 : label_idx + 6]:
        if "合计" in line:
            val = _val_in(line)
            if val:
                return val, f"{label.strip()} … {line.strip()}"
    # 单级基金：数值就在标签行上
    val = _val_in(label[label.find("基金经理") :])
    if val:
        return val, label.strip()
    # 标签被折行且无合计行：取随后最近的区间值
    for line in lines[label_idx + 1 : label_idx + 4]:
        val = _val_in(line)
        if val:
            return val, f"{label.strip()} … {line.strip()}"
    return None


def _find_employees_exact(lines):
    """提取“从业人员持有本基金”的精确份额总数（新格式报表披露）。

    数值必须出现在标签之后，避免误抓章节编号（如“8.2 期末基金管理人的…”）。
    分级基金的标签会被折行，此时取其后的“合计”行（精确份额带千分位和百分比）。
    """
    for line in lines:
        if "从业人员持有本基金" not in line or _TOC_LINE.search(line):
            continue
        after = line.split("从业人员持有本基金", 1)[1]
        m = re.search(r"\d[\d,，]*(?:\.\d+)?", after)
        if m:
            pct = re.search(r"[\d.]+%", after)
            return {"shares": m.group(0), "pct": pct.group(0) if pct else ""}
    # 标签折行（如“基金管理人所有从业人”/“员持有本基金”）：找随后的合计行
    for j, line in enumerate(lines):
        if _TOC_LINE.search(line) or "从业" not in line or "的情况" in line:
            continue  # 跳过章节标题行，锚定真正的表格标签
        for l2 in lines[j + 1 : j + 8]:
            if "合计" in l2:
                m = re.search(r"\d{1,3}(?:[,\s，]\d{3})+(?:\.\d+)?", l2)
                pct = re.search(r"[\d.]+%", l2)
                if m and pct:
                    return {"shares": m.group(0), "pct": pct.group(0)}
        break
    return None


def parse_manager_holding(pdf_path):
    """解析 PDF，返回基金经理持有信息；未找到返回 None"""
    best = None  # 只有从业人员数据时也保留（经理区间可能以文字表述漏检）
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        n = len(pages)
        # “基金份额持有人信息”章节一般在报告后段（约 55% 之后）
        for i in range(int(n * 0.5), n):
            text = pages[i].extract_text() or ""
            if not _HEADING_PAT.search(text):
                continue
            lines = text.split("\n")
            # 章节可能跨页：窗口取标题之后的整页 + 下一页前 40 行，
            # 否则碎片化版式（如 000242）的经理“合计”行会落在窗口外
            nxt = (pages[i + 1].extract_text() or "").split("\n")[:40] if i + 1 < n else []
            for idx, line in enumerate(lines):
                if _HEADING_PAT.search(line) and not _TOC_LINE.search(line):
                    window = lines[idx:] + nxt
                    mgr = _find_manager_line(window)
                    emp = _find_employees_exact(window)
                    if mgr:
                        return {
                            "manager_range": mgr[0],
                            "manager_line": mgr[1],
                            "employees_exact": emp,
                            "page": i + 1,
                            "snippet": "\n".join(window[:18]),
                        }
                    if emp and best is None:
                        best = {
                            "manager_range": "",
                            "manager_line": "",
                            "employees_exact": emp,
                            "page": i + 1,
                            "snippet": "\n".join(window[:18]),
                        }
    return best


def format_range(val):
    """把区间值格式化为展示文案，如 '>100' -> '>100万份'"""
    if not val:
        return ""
    v = val.strip()
    if v.endswith("万份"):
        return v
    if v == "0":
        return "0（未持有）"
    return f"{v}万份"


_CACHE_VER = 7  # 解析/缓存策略变更时 +1，让旧缓存自动失效


def get_manager_holding(client, code):
    """完整链路：公告列表 -> 最新中报/年报 -> 下载 PDF -> 解析。结果按公告ID永久缓存。

    只有确定性结果（解析成功/无报告/解析失败）才缓存；网络类错误（FundApiError，
    如风控重置连接、超时）不缓存，否则一次网络抖动会让该基金 7 天内一直报错。
    """
    cache = client.cache
    hit = cache.get(f"holding_v{_CACHE_VER}", code, ttl=7 * 86400)
    if hit is not None:
        return hit

    result = {"status": "error", "error": "未知错误"}
    try:
        notices = client.periodic_notices(code)
        reports = [
            it
            for it in notices
            if ("年度报告" in it.get("TITLE", "") or "中期报告" in it.get("TITLE", ""))
            and "提示" not in it.get("TITLE", "")
            and "摘要" not in it.get("TITLE", "")
        ]
        if not reports:
            result = {"status": "no_report", "error": "该基金暂无年度报告/中期报告披露"}
        else:
            rep = reports[0]  # 列表本身按发布时间倒序
            art_code = rep["ID"]
            parsed = cache.get(f"holding_parse_v{_CACHE_VER}", art_code)
            if parsed is None:
                detail = client.notice_detail(art_code)
                attaches = detail.get("attach_list") or []
                url = (attaches[0] or {}).get("attach_url") if attaches else detail.get("attach_url")
                if not url:
                    raise RuntimeError("公告没有 PDF 附件")
                pdf_path = client.download_pdf(url, f"{art_code}.pdf")
                parsed = parse_manager_holding(pdf_path) or {}
                parsed.setdefault("manager_range", "")
                parsed.setdefault("manager_line", "")
                parsed.setdefault("employees_exact", None)
                parsed.setdefault("page", 0)
                parsed.setdefault("snippet", "")
                if not parsed.get("manager_range") and not parsed.get("employees_exact"):
                    parsed["parse_failed"] = True
                parsed["pdf_url"] = url
                cache.set(f"holding_parse_v{_CACHE_VER}", art_code, parsed)
            result = {
                "status": "ok",
                "report_title": rep.get("TITLE", ""),
                "report_date": rep.get("PUBLISHDATEDesc", ""),
                **parsed,
            }
    except FundApiError as e:  # 网络类错误：不缓存，下次查询直接重试
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 —— 任何环节失败都要在界面上给出可读的错误
        result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    cache.set(f"holding_v{_CACHE_VER}", code, result)
    return result
