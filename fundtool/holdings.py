# -*- coding: utf-8 -*-
"""从基金定期报告（年度报告/中期报告）PDF 中提取“基金经理持有本基金”的份额区间，
以及“基金管理人所有从业人员持有本基金”的精确份额总数。

披露口径（证监会《公开募集证券投资基金信息披露内容与格式准则》）：
- 中报/年报的“基金份额持有人信息”章节披露期末基金管理人从业人员持有本基金的情况；
- “本基金基金经理持有本开放式基金”一行给出区间（万份）：0 / 0-10 / 10-50 / 50-100 / >100；
  部分旧格式报表的行名为“基金经理等人员”；
- “基金管理人所有从业人员持有本基金”一行给出精确份额总数（份）与占基金总份额比例；
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
# 章节标题行：行首编号 + 中文标题（如“8.5 发起式基金发起资金持有份额情况”），
# 编号（8.5）不是区间值，不得参与 _class_map 取值/配对
_SECTION_HEAD = re.compile(r"^\s*\d+(?:\.\d+)*\s*[\u4e00-\u9fa5§]")

# 数值/区间：0、10-50、>100、＞100 等
_NUM = r"[<>＞＜]?\s*\d[\d,，]*(?:\.\d+)?(?:\s*[-~～—至]\s*\d[\d,，]*(?:\.\d+)?)?"
# 区间表里的合法值（万份）：必须是独立词块（前后为空白/行尾/括号），
# 小数不超过两位、不含千分位——排除“2026年中期报告”的年份、“§9”的章节号、
# “第 45页”的页码以及 8,038,597.09 这类精确份额数（数字后紧跟 逗号+数字 是千分位前缀，不是区间值，
# 如 001791 的 8.5 表“基金经理等人员 603,826.10”，603 不得被当成区间）
_NUM_TOKEN = re.compile(
    r"(?:^|[\s（(])([<>＞＜]?\d{1,4}(?:\.\d{1,2})?(?:[-~～—至]\d{1,4}(?:\.\d{1,2})?)?)(?:万?份)?(?=[\s)）、]|$|,(?!\d)|，(?!\d))"
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


def _last_class_mark(line):
    """行内最后一个"独立"的份额级别字母（A/C/E…，可带"类"），如行尾 ' C'、
    独立一行 'A'、名称内结尾 '海富通均衡甄选混合C'。无则返回 None"""
    m = None
    for mm in re.finditer(r"([A-Z])类?(?=[\s)）、,，]|$)", line):
        m = mm.group(1)
    return m


def _class_map(lines, lo, hi):
    """从行区间构建 {份额级别: 区间值}（含'合计'）。

    值与级别字母同行为准；独行值就近与 ±1 行内的级别字母配对
    （PDF 抽行会把 份额级别/区间 两列的顺序打乱）。
    """
    marks, vals, result = [], [], {}
    for i in range(lo, hi):
        line = lines[i]
        if _TOC_LINE.search(line) or _SECTION_HEAD.match(line):
            continue
        if "合计" in line:
            t = _val_in(line) or (i + 1 < len(lines) and _val_in(lines[i + 1])) or ""
            if t:
                result["合计"] = t
            continue
        v = _val_in(line)
        if v:
            vals.append((i, v))
        m = _last_class_mark(line)
        if m:
            marks.append((i, m))
    for vi, v in vals:
        best, bd = None, 3
        for mi, m in marks:
            d = abs(mi - vi)
            if d < bd:
                bd, best = d, m
        if best:
            result[best] = v
    return result


def _find_manager_line(lines, share_class=None):
    """定位「本基金基金经理持有本开放式基金」标签的值。返回 (区间值, 说明行, 各份额级别映射或 None)。

    只认基金经理**本人**持有的标准栏名，不用「基金经理等人员」「高级管理人员」
    等其他口径的行代替——那些不是同一种数据，宁缺勿错（找不到返回 None，界面显示 --）。

    标准栏名允许的变形（PDF 折行/表格列打断）：
    - 同行完整："本基金基金经理持有本开放式基金 >100"
    - 折行："…持有本开 / 放式基金 10-50"（中间还可能插着份额级别列的行，如 010790）
    - 被份额级别列打断（分级基金）："本基金基金经理持有 大成…A 50~100 / 本开放式基金 …C 0"
    锚点为子串「基金经理持」——窗口内其他口径的行都不含它：
    「高级管理人员、基金经理投资…负责人」是"基金经理投"、「基金经理等人员」是"基金经理等"。

    其他版式：
    - 多级基金：优先取查询代码所属份额类别的行；类别行缺失或未指定时取"合计"行
    - 占位"-"：表示未持有，按 0 处理（如 018554）
    - 文字表述版式："……本基金基金经理未持有本基金。"（句子可能在"未持/有"之间折行）
    """
    label_idx = None
    for j, line in enumerate(lines):
        if _TOC_LINE.search(line) or "基金经理持" not in line:
            continue
        label_idx = j
        break
    if label_idx is None:
        # 文字表述版式：先看“未持有”（未持有=0 也是基金经理本人持有的口径）
        for j, line in enumerate(lines):
            if _TOC_LINE.search(line) or "基金经理" not in line:
                continue
            joined = line + (lines[j + 1] if j + 1 < len(lines) else "")
            if "未持有" in joined:
                return "0", joined.strip(), None
        return None
    label = lines[label_idx]
    cmap = _class_map(lines, max(0, label_idx - 3), min(len(lines), label_idx + 6))
    if share_class and cmap.get(share_class):
        return cmap[share_class], f"{label.strip()} … {share_class}类 {cmap[share_class]}", cmap
    if cmap.get("合计"):
        return cmap["合计"], f"{label.strip()} … 合计 {cmap['合计']}", cmap or None
    # 单级基金：数值就在标签行上
    val = _val_in(label[label.find("基金经理") :])
    if val:
        return val, label.strip(), cmap or None
    # 标签被折行且无合计行：取随后最近的区间值
    for line in lines[label_idx + 1 : label_idx + 4]:
        val = _val_in(line)
        if val:
            return val, f"{label.strip()} … {line.strip()}", cmap or None
    return None


# 9.3 区间小节标题特征（“……持有本开放式基金份额总量区间情况”）：9.2 小节的截止线
_SEC_RANGE_HEAD = re.compile(r"区间情况")
# 精确数值（份额/占比）：千分位分隔或带小数；区间档位值（0 / 10~50 / >100）不匹配
_PRECISE_NUM = re.compile(r"\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?|\d+\.\d+")


def _line_two_nums(text):
    """行内前两个精确数值（份额、占比），按列位置区分——占比列的 % 常只在表头，
    值是裸数字（如“合计 552,727.27 0.06986”），不能靠 % 字符判断。无则返回 (None, None)"""
    nums = _PRECISE_NUM.findall(text)
    if not nums:
        return None, None
    return nums[0], (nums[1] if len(nums) > 1 else "")


def _employees_class_rows(sec):
    """9.2 小节内各份额级别的从业人员行：{级别字母: {'shares','pct'}}（不含合计行）。
    级别字母取行内最后一个独立大写字母（可带“类”），与经理区间表的 _class_map 同款规则；
    9.3 区间表的级别行是档位值（0~10/>100），_line_two_nums 不匹配，不会混入。"""
    out = {}
    for l in sec:
        if "合计" in l or "的情况" in l or _TOC_LINE.search(l):
            continue
        m = _last_class_mark(l)
        if not m:
            continue
        shares, pct = _line_two_nums(l)
        if shares:
            out[m] = {"shares": shares, "pct": pct}
    return out


def _find_employees_exact(lines):
    """提取「基金管理人所有从业人员持有本基金」的精确份额总数与占比，
    及各份额级别明细（classes，无级别行时省略该键）。

    标签常被 PDF 折行打碎（基金管/理人所/有从业/人员持/有本基金 各自成行、
    与份额级别行交错），靠标签锚定不可行——改为锚定含「从业」的行（小节标题
    或标签残片），窗口止于「区间情况」标题或下一编号标题：
    - 分级基金取「合计」行（全基金口径；9.3 的区间合计是 0~10/>100 档位，
      _PRECISE_NUM 不匹配，误入也不会取到）；
    - 单级基金取完整标签行「…从业人员持有本基金 8,038,597.09 0.05%»，
      数值可能折到下一行（标题行以“的情况”结尾，自动跳过）；
    - 标签断在「持/有」或「从业人|员持有」之间时，数值与残片同行或独立成行：
      「…从业人员持 / 894,804.86 0.11% / 有本基金」「…所有从业人 / 886,488.66 0.1483% / 员持有本基金」；
    - 文字版式「基金管理人的从业人员未持有本基金。」或小节正文仅「无。」按 0 处理。
    """
    for start, line in enumerate(lines):
        if "从业" not in line or _TOC_LINE.search(line):
            continue
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _SEC_RANGE_HEAD.search(lines[j]) or _SECTION_HEAD.match(lines[j]):
                end = j
                break
        sec = lines[start:end]
        classes = _employees_class_rows(sec)
        for l in sec:  # 分级基金：「合计」行
            if "合计" in l and not _TOC_LINE.search(l):
                shares, pct = _line_two_nums(l)
                if shares:
                    out = {"shares": shares, "pct": pct}
                    if classes:
                        out["classes"] = classes
                    return out
        for k, l in enumerate(sec):  # 单级基金：完整标签行
            if "从业人员持有本基金" not in l or "的情况" in l or _TOC_LINE.search(l):
                continue
            after = l.split("从业人员持有本基金", 1)[1]
            if not _PRECISE_NUM.search(after) and k + 1 < len(sec):  # 数值折行
                after += " " + sec[k + 1]
            shares, pct = _line_two_nums(after)
            if shares:
                out = {"shares": shares, "pct": pct}
                if classes:
                    out["classes"] = classes
                return out
        for mark in ("从业人员持", "所有从业"):  # 标签残片行：数值同行或在下一行
            for k, l in enumerate(sec):
                if mark not in l or "的情况" in l or _TOC_LINE.search(l):
                    continue
                after = l.split(mark, 1)[1]
                if not _PRECISE_NUM.search(after) and k + 1 < len(sec):
                    after += " " + sec[k + 1]
                shares, pct = _line_two_nums(after)
                if shares:
                    out = {"shares": shares, "pct": pct}
                    if classes:
                        out["classes"] = classes
                    return out
        for l in sec:  # 文字版式：从业人员未持有 / 小节正文「无。」
            if ("从业" in l and "未持有" in l) or l.strip() in ("无。", "无"):
                return {"shares": "0.00", "pct": ""}
    return None


def parse_manager_holding(pdf_path, share_class=None):
    """解析 PDF，返回基金经理持有信息；未找到返回 None。

    share_class：查询代码所属份额级别（如 'A'/'C'，来自基金简称末尾字母），
    多级别披露的报表取该级别的行；None 或该级别行缺失时取合计。
    """
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
                    mgr = _find_manager_line(window, share_class)
                    emp = _find_employees_exact(window)
                    if mgr:
                        val, mline, cmap = mgr
                        out = {
                            "manager_range": val,
                            "manager_line": mline,
                            "employees_exact": emp,
                            "page": i + 1,
                            "snippet": "\n".join(window[:18]),
                        }
                        if cmap and len(cmap) > 1:
                            out["manager_ranges"] = cmap
                        return out
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


_CACHE_VER = 10  # 解析/缓存策略变更时 +1，让旧缓存自动失效（v10：只认“本基金基金经理持有本开放式基金”标准栏名，不再用“基金经理等人员”等替代）

# 区间档位：经理持有是区间披露，档位有序，用于两期对比
_RANK = {"0": 0, "0-10": 1, "10-50": 2, "50-100": 3, ">100": 4}


def range_rank(val):
    """区间值 -> 档位（0~4），无法识别返回 None"""
    if not val:
        return None
    v = _norm(str(val)).replace("万份", "")
    for ch in "~～—至":
        v = v.replace(ch, "-")
    return _RANK.get(v)


def range_change(cur, prev):
    """两期区间对比：'持平' / '升N档' / '降N档'；任一期无法识别返回 None"""
    a, b = range_rank(cur), range_rank(prev)
    if a is None or b is None:
        return None
    d = a - b
    if d == 0:
        return "持平"
    return ("升" if d > 0 else "降") + f"{abs(d)}档"


def _periodic_reports(notices):
    """公告列表中的正式中报/年报（列表已按发布时间倒序）"""
    return [
        it
        for it in notices
        if ("年度报告" in it.get("TITLE", "") or "中期报告" in it.get("TITLE", ""))
        and "提示" not in it.get("TITLE", "")
        and "摘要" not in it.get("TITLE", "")
    ]


def _share_class(client, code):
    """查询代码所属份额级别：基金简称末尾的大写字母（如 XX混合A -> 'A'）。
    多级别报表按此取对应行；无后缀（单级别基金）返回 None。"""
    try:
        name = (client.basic_info(code) or {}).get("SHORTNAME") or ""
    except FundApiError:
        return None
    m = re.search(r"([A-Z])$", name.strip())
    return m.group(1) if m else None


def _parse_report(client, rep, share_class=None):
    """下载并解析单份定期报告（按 公告ID+份额类别 永久缓存）。rep：公告列表项。"""
    cache = client.cache
    art_code = rep["ID"]
    cache_key = f"{art_code}_{share_class or 'ALL'}"
    parsed = cache.get(f"holding_parse_v{_CACHE_VER}", cache_key)
    if parsed is None:
        detail = client.notice_detail(art_code)
        attaches = detail.get("attach_list") or []
        url = (attaches[0] or {}).get("attach_url") if attaches else detail.get("attach_url")
        if not url:
            raise RuntimeError("公告没有 PDF 附件")
        pdf_path = client.download_pdf(url, f"{art_code}.pdf")
        parsed = parse_manager_holding(pdf_path, share_class) or {}
        parsed.setdefault("manager_range", "")
        parsed.setdefault("manager_line", "")
        parsed.setdefault("employees_exact", None)
        parsed.setdefault("page", 0)
        parsed.setdefault("snippet", "")
        if not parsed.get("manager_range") and not parsed.get("employees_exact"):
            parsed["parse_failed"] = True
        parsed["pdf_url"] = url
        cache.set(f"holding_parse_v{_CACHE_VER}", cache_key, parsed)
    return parsed


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
        reports = _periodic_reports(client.periodic_notices(code))
        if not reports:
            result = {"status": "no_report", "error": "该基金暂无年度报告/中期报告披露"}
        else:
            rep = reports[0]  # 列表本身按发布时间倒序
            parsed = _parse_report(client, rep, _share_class(client, code))
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


def get_manager_holding_history(client, code, n=2):
    """最近 n 份中报/年报的经理持有（新→旧），每期含环比 change（'持平'/'升N档'/'降N档'）。

    每份报告的解析按公告ID永久缓存，因此只有新报告才需要下载解析。
    网络类错误（FundApiError）不缓存直接抛出；无报告返回 []。
    """
    cache = client.cache
    hit = cache.get(f"holding_hist_v{_CACHE_VER}", f"{code}_n{n}", ttl=7 * 86400)
    if hit is not None:
        return hit

    reports = _periodic_reports(client.periodic_notices(code))[:n]
    share_class = _share_class(client, code)
    result = []
    for rep in reports:
        parsed = {}
        try:
            parsed = _parse_report(client, rep, share_class)
            rng = parsed.get("manager_range") or ""
        except FundApiError:
            raise  # 网络错误不缓存
        except Exception:  # noqa: BLE001 —— 单份报告解析失败不拖垮整段历史
            rng = ""
        result.append({
            "report_title": rep.get("TITLE", ""),
            "report_date": rep.get("PUBLISHDATEDesc", ""),
            "manager_range": rng,
            "pdf_url": parsed.get("pdf_url", ""),
            "change": "",
        })
    for i, r in enumerate(result):
        prev = result[i + 1]["manager_range"] if i + 1 < len(result) else ""
        r["change"] = range_change(r["manager_range"], prev) or ""
    cache.set(f"holding_hist_v{_CACHE_VER}", f"{code}_n{n}", result)
    return result
