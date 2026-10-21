# -*- coding: utf-8 -*-
"""天天基金（东方财富）数据客户端：基金基本信息、基金经理任职、基金经理目录、定期报告公告、报告 PDF 下载、基金搜索"""
import json
import os
import re
import time

import requests

from .jschallenge import looks_like_challenge, solve

# 注意：fundmobapi 的风控会拒绝带 AppleWebKit 字样的 UA，这里必须用简短 UA
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# 重仓股 PE 统计的微利提示阈值（倍）：PE-TTM 超过该值的股票盈利近乎为零（如茂莱光学
# 11.7万倍），计入加权/平均（忠实反映持仓），但界面琥珀底提示该基金"含微利股"——
# 极端市盈率会把数字显著放大，跨基金比较时需留意
PE_EXTREME = 500.0


class FundApiError(Exception):
    pass


def _parse_js_object(text):
    """解析 `var returnjson= {data:[...],pages:9}` 这类键不带引号的非严格 JSON"""
    body = text[text.find("{"):text.rfind("}") + 1]
    body = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', body)
    return json.loads(body)


def _search_rel_rank(name, kw):
    """搜索结果相关度：精确匹配 > 名称以关键词开头 > 与关键词公共前缀≥4字 > 包含关键词 > 其他。
    接口自身的排序按词频覆盖度，常把"中欧创新成长灵活配置"排到"华商创新成长"前面。"""
    if name == kw:
        return 0
    if name.startswith(kw):
        return 1
    if kw.startswith(name):
        return 2
    p = 0
    for a, b in zip(name, kw):
        if a != b:
            break
        p += 1
    if p >= 4:
        return 3
    if kw in name:
        return 4
    return 5


# 阶段涨幅接口的 title -> 行字段：Y/3Y/6Y=近1/3/6月（月=yue），1N/2N/3N/5N=近1/2/3/5年（年=nian），
# JN=今年以来（Z=近1周、LN=成立来不用）
_PERIOD_KEYS = {"Y": "r1m", "3Y": "r3m", "6Y": "r6m", "1N": "r1y", "2N": "r2y", "JN": "rytd", "3N": "r3y", "5N": "r5y"}

_PERIOD_FIELDS = tuple(_PERIOD_KEYS.values())


def parse_period_returns(datas):
    """阶段涨幅接口的 Datas 列表 -> {近1月r1m/近3月r3m/近6月r6m/今年rytd/近1年r1y/近2年r2y/近3年r3y/近5年r5y: float|None}（单位 %）。
    纯函数，便于离线测试。syl 为空串（新基金未满该期）或非数字时记 None。"""
    out = dict.fromkeys(_PERIOD_FIELDS)
    for it in datas or []:
        key = _PERIOD_KEYS.get(it.get("title"))
        if not key:
            continue
        v = it.get("syl")
        try:
            out[key] = float(v)
        except (TypeError, ValueError):
            pass
    return out


# F10 阶段涨幅页（jdzf）的周期标题 -> 字段（「今年来」与移动端的「今年以来」同义；近1周/成立来不用）
_JDZF_TITLES = {"近1月": "r1m", "近3月": "r3m", "近6月": "r6m", "今年来": "rytd",
                "近1年": "r1y", "近2年": "r2y", "近3年": "r3y", "近5年": "r5y"}

# F10 费率页（jjfl_*.html）运作费表的两种版式：常规横排 <td class="th w110">管理费率</td><td class="w135">1.20%（每年）</td>；
# 浮动费率竖排 <td class="yzfth w110">管理费率</td><td class="yzfcont"...><pre>多档文本</pre></td>
_FEE_TD = r'</td><td class="(?:w135|yzfcont[^"]*)"[^>]*>(.*?)</td>'


def _fee_text(v):
    """费率单元格 HTML -> 干净原文（去标签、换行压成空格）"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", v or "")).strip()


def _fee_pct(text):
    """费率原文 -> (数值|None, 是否多档浮动)：取文本中最高档百分比（如浮动管理费
    '高于基准:1.60%;等于:1.20%;低于:0.80%' -> 1.60）；无百分比（'---'）-> (None, False)"""
    vals = {float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*%", text or "")}
    if not vals:
        return None, False
    return max(vals), len(vals) > 1


def calc_pe_stats(stocks):
    """重仓股 PE 统计（纯函数，便于离线测试）。输入 [{code,name,pct,pe}]：
    pct=占净值比例（%）、pe=PE-TTM。亏损/无PE（pe<=0 或 None，记 loss_n）不参与计算；
    微利异常（pe>PE_EXTREME，记 extreme_n）**照常计入**加权/平均（忠实反映持仓），
    但界面会提示该数字被极端市盈率显著放大。
    返回 {weighted, average, loss_n, extreme_n, valid_n, covered_pct}：
    - weighted 按占净值比例加权 Σ(pct*pe)/Σpct（仅有效股，pct 缺按等权处理）
    - average  有效股简单算术平均 Σpe/n
    - covered_pct 重仓股合计占净值比例（%），亏损股也计入（口径透明）
    无有效股时 weighted/average 为 None。"""
    valid = [s for s in stocks or [] if s.get("pe") is not None and s["pe"] > 0]
    extreme_n = sum(1 for s in valid if s["pe"] > PE_EXTREME)
    loss_n = len(stocks or []) - len(valid)
    covered = sum(s.get("pct") or 0.0 for s in stocks or [])
    out = {"weighted": None, "average": None, "loss_n": loss_n, "extreme_n": extreme_n,
           "valid_n": len(valid), "covered_pct": round(covered, 2)}
    if not valid:
        return out
    out["average"] = round(sum(s["pe"] for s in valid) / len(valid), 2)
    if all(s.get("pct") for s in valid):  # 全部有占比才按权重加权，否则退化为等权平均
        tw = sum(s["pct"] for s in valid)
        out["weighted"] = round(sum(s["pct"] * s["pe"] for s in valid) / tw, 2)
    else:
        out["weighted"] = out["average"]
    return out


# 重仓股交易所 TEXCH -> 行情接口 secid 市场前缀（1=沪 2=深 5=港；北交所等未覆盖的记 None 不查 PE）
_PE_MARKET = {"1": "1", "2": "0", "5": "116"}

# 基金份额级别后缀字母（A/B/C/D/E/H/I/R/Y 等）：母基金聚合时从简称末尾剥离。
# 刻意不含 F/L/T——ETF/LOF/FOF 本身以 F 结尾，剥了会把"XX ETF"切成"XX ET"
SHARE_CLASS_LETTERS = frozenset("ABCDEHIRY")


def parent_fund_key(name):
    """基金简称 -> 母基金聚合键：剥离末尾的单个份额级别字母（'南方致远混合C' -> '南方致远混合'）。
    无份额后缀（含 ETF/LOF/FOF 结尾、纯中文名）原样返回。纯函数，便于测试。"""
    s = str(name or "").strip()
    if len(s) >= 5 and s[-1] in SHARE_CLASS_LETTERS:
        return s[:-1]
    return s


def aggregate_fof_holdings(fof_rows):
    """FOF 持仓反向聚合（纯函数，便于测试）。输入 FOF 行列表：
    [{code, name, type, holdings: [{code,name,pct}]}]（holdings 为 fof_holdings 的返回），
    输出按被持基金**母基金**（parent_fund_key）聚合的结果，按持有 FOF 数降序：
    [{key, rep_code, rep_name, holders: [{fof_code, fof_name, fof_type,
                                          shares: [{code,name,pct}…], pct}]}]
    - 同一 FOF 同时持有某基金的 A/C 份额只算 1 个持有人（shares 记全部份额，pct 取最高档）
    - rep_code/rep_name 取被持次数最多的份额（展示代表份额）
    """
    agg = {}  # parent_key -> {"shares": {code: {"name", "n", "pct_sum"}}, "fofs": {fof_code: {...}}}
    for f in fof_rows or []:
        for h in f.get("holdings") or []:
            if not h.get("code"):
                continue
            key = parent_fund_key(h.get("name"))
            slot = agg.setdefault(key, {"shares": {}, "fofs": {}})
            sh = slot["shares"].setdefault(h["code"], {"name": h.get("name") or h["code"], "n": 0})
            sh["n"] += 1
            holder = slot["fofs"].setdefault(f["code"], {
                "fof_code": f["code"], "fof_name": f.get("name") or f["code"],
                "fof_type": f.get("type") or "", "shares": [], "pct": None})
            holder["shares"].append({"code": h["code"], "name": h.get("name") or h["code"],
                                     "pct": h.get("pct")})
            p = h.get("pct")
            if p is not None and (holder["pct"] is None or p > holder["pct"]):
                holder["pct"] = p
    out = []
    for key, slot in agg.items():
        rep_code = max(slot["shares"], key=lambda c: slot["shares"][c]["n"])
        out.append({
            "key": key,
            "rep_code": rep_code,
            "rep_name": slot["shares"][rep_code]["name"],
            "holders": sorted(slot["fofs"].values(), key=lambda h: -(h["pct"] or 0)),
        })
    out.sort(key=lambda r: (-len(r["holders"]), -max((h["pct"] or 0) for h in r["holders"])))
    return out


def parse_fee_page(text):
    """F10 费率页 HTML -> {mng/cust/sales: 数值|None, *_text: 原文, total, floating,
    buy_orig/buy_disc: 申购费首档原价/天天折扣, redeem: [[持有期限, 费率]…]}。
    total = 三项运作费之和（%/年，缺项按 0，全缺为 None）；任一项多档浮动记 floating。
    申购费取「小于100万元」首档（普通投资者适用档），strike 删除线为原价、竖线后为天天优惠价。
    纯函数，便于离线测试。"""
    out = {"mng_text": "", "cust_text": "", "sales_text": "",
           "mng": None, "cust": None, "sales": None,
           "total": None, "floating": False, "buy_orig": "", "buy_disc": "", "redeem": []}
    for label, key in (("管理费率", "mng"), ("托管费率", "cust"), ("销售服务费率", "sales")):
        m = re.search(label + _FEE_TD, text or "", re.S)
        if not m:
            continue
        raw = _fee_text(m.group(1))
        out[key + "_text"] = raw
        out[key], floating = _fee_pct(raw)
        out["floating"] = out["floating"] or floating
    vals = [out[k] for k in ("mng", "cust", "sales")]
    if any(v is not None for v in vals):
        out["total"] = round(sum(v for v in vals if v is not None), 2)
    # 申购费率表（与「认购费率」同结构，按 <label>锚定区分）：首行原价（strike 删除线）| 天天折扣价
    m = re.search(r'<label class="left">申购费率.*?<tbody>(.*?)</tbody>', text or "", re.S)
    if m:
        row1 = re.search(r"<tr>(.*?)</tr>", m.group(1), re.S)
        if row1:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row1.group(1), re.S)
            if len(cells) >= 2:
                sm = re.search(r"<strike[^>]*>(.*?)</strike>(?:&nbsp;)*\|(?:&nbsp;)*(.*?)$", cells[1], re.S)
                if sm:
                    out["buy_orig"], out["buy_disc"] = _fee_text(sm.group(1)), _fee_text(sm.group(2))
                else:  # C 类 0.00%、固定每笔费用等无删除线版式
                    out["buy_orig"] = _fee_text(cells[1])
    # 赎回费梯度表：适用期限 / 赎回费率
    m = re.search(r'<label class="left">赎回费率.*?<tbody>(.*?)</tbody>', text or "", re.S)
    if m:
        for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(cells) >= 2:
                out["redeem"].append([_fee_text(cells[0]), _fee_text(cells[1])])
    return out


def parse_jdzf_ranks(text):
    """F10 阶段涨幅页 HTML -> {各期: 同类排名百分位 float|None}（0=最好，100=最差）。
    同类排名单元格形如 <li class='tlpm'>4573<font class='gray'>|</font>4787</li>
    （第 1 名=最好；同类口径为天天基金二级分类，如混合型-偏股）。
    未满该期或成立来（'---'）无此结构，记 None。纯函数，便于离线测试。"""
    out = dict.fromkeys(_JDZF_TITLES.values())
    for block in text.split("<ul")[1:]:
        tm = re.search(r"<li class='title'>([^<]+)</li>", block)
        key = _JDZF_TITLES.get(tm.group(1)) if tm else None
        if not key:
            continue
        rm = re.search(r"tlpm'>(\d+)<font class='gray'>\|</font>(\d+)<", block)
        if rm:
            rank, total = int(rm.group(1)), int(rm.group(2))
            if 0 < rank <= total:
                out[key] = round(rank / total * 100, 2)
    return out


class EastFundClient:
    def __init__(self, cache, rate_limit=0.25, timeout=20):
        self.cache = cache
        self.rate_limit = rate_limit
        self.timeout = timeout
        self.pdf_dir = os.path.join(cache.base, "pdfs")
        os.makedirs(self.pdf_dir, exist_ok=True)
        self.session = requests.Session()
        # 数据源均为国内站点（eastmoney/dfcfw）。忽略系统代理：代理软件开启时
        # 请求经境外节点会被天天基金风控重置连接（ConnectionResetError 10054）
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": _UA, "Referer": "https://fund.eastmoney.com/"})
        self._last_req = 0.0

    # ---------- 基础设施 ----------
    def _throttle(self):
        wait = self.rate_limit - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _get(self, url, params=None, timeout=None):
        last_err = None
        for attempt in range(4):
            try:
                self._throttle()
                r = self.session.get(url, params=params, timeout=timeout or self.timeout)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_err = e
                # 连接被远端重置（ConnectionResetError 10054）多为风控临时拦截，
                # 立即重试仍会被重置，需要更长冷却；其他错误用短退避。
                if isinstance(e, requests.ConnectionError):
                    time.sleep(5.0 + 5.0 * attempt)
                else:
                    time.sleep(1.0 + 2.0 * attempt)
        raise FundApiError(f"请求失败: {url} ({last_err})")

    # ---------- 业务接口 ----------
    def basic_info(self, code):
        """基金基本信息：名称/类型/基金经理/规模/净值/公司等；代码不存在返回 None"""
        cached = self.cache.get("basic", code, ttl=12 * 3600)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation",
            params={
                "FCODE": code,
                "deviceid": "1",
                "plat": "Iphone",
                "product": "EFund",
                "version": "6.2.5",
            },
        )
        datas = (r.json() or {}).get("Datas")
        if not datas or not datas.get("SHORTNAME"):
            return None
        self.cache.set("basic", code, datas)
        return datas

    def period_returns(self, code):
        """阶段涨幅（%）：近1月/近3月/近6月/今年以来/近1年/近2年/近3年/近5年
        （r1m/r3m/r6m/rytd/r1y/r2y/r3y/r5y），各期 float|None（新基金未满该期为 None）。
        与基本信息同样缓存 12 小时。缓存 v3：v2 及更早的旧缓存缺 r2y，升键强制重取。"""
        cached = self.cache.get("period_v3", code, ttl=12 * 3600)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNPeriodIncrease",
            params={
                "FCODE": code,
                "deviceid": "1",
                "plat": "Iphone",
                "product": "EFund",
                "version": "6.2.5",
            },
        )
        out = parse_period_returns((r.json() or {}).get("Datas"))
        self.cache.set("period_v3", code, out)
        return out

    def peer_rank(self, code):
        """各期同类排名百分位（F10 阶段涨幅页）：{r1m/r3m/r6m/rytd/r1y/r2y/r3y/r5y: float|None}，
        0=最好 100=最差（如 4573/4787 名 -> 95.53）。同类口径为天天基金二级分类，每日更新；
        缓存 12 小时（与阶段涨幅一致）。"""
        cached = self.cache.get("peerrank_v1", code, ttl=12 * 3600)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
            params={"type": "jdzf", "code": code},
        )
        out = parse_jdzf_ranks(r.text)
        self.cache.set("peerrank_v1", code, out)
        return out

    def fee_info(self, code):
        """F10 费率页（jjfl_*.html）：管理/托管/销售服务费率（原文+数值+合计）、
        申购费首档（原价/天天折扣）、赎回费梯度，字段见 parse_fee_page。
        费率极少变动，缓存 30 天。附加信息，调用方失败不致命。"""
        cached = self.cache.get("fees_v1", code, ttl=30 * 86400)
        if cached is not None:
            return cached
        r = self._get(f"https://fundf10.eastmoney.com/jjfl_{code}.html")
        out = parse_fee_page(r.text)
        self.cache.set("fees_v1", code, out)
        return out

    def fund_holdings_pe(self, code):
        """基金重仓股估值：移动端持仓接口（最新季报前十大重仓股，含港股市值占比）
        + 行情接口批量取 PE-TTM，合成明细与统计。返回：
        {stocks: [{code,name,pct,pe}], weighted, average, loss_n, extreme_n, valid_n, covered_pct}
        （字段含义见 calc_pe_stats；亏损/无PE 不参与计算，微利异常股计入并由界面提示）。
        持仓季度更新但 PE 随行情变动，整体缓存 12 小时（与阶段涨幅一致）；
        缓存 v3：v2 曾把微利异常股剔除出统计，按用户口径改回复实计入+提示，升键强制重算。
        无重仓股（债基/货基/未披露）返回 {stocks: []}。附加信息，失败不致命。"""
        cached = self.cache.get("hpe_v3", code, ttl=12 * 3600)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition",
            params={"FCODE": code, "deviceid": "1", "plat": "Iphone",
                    "product": "EFund", "version": "6.2.5"},
        )
        rows = ((r.json() or {}).get("Datas") or {}).get("fundStocks") or []
        stocks = []
        for it in rows:
            pct = None
            try:
                pct = float(it.get("JZBL"))
            except (TypeError, ValueError):
                pass
            stocks.append({"code": str(it.get("GPDM") or ""), "name": it.get("GPJC") or "",
                           "pct": pct, "secid": None, "pe": None})
        # 批量查 PE-TTM（f115）：亏损为负数、无效代码自动缺席
        secids = []
        for s, it in zip(stocks, rows):
            mkt = _PE_MARKET.get(str(it.get("TEXCH") or ""))
            if mkt and s["code"]:
                s["secid"] = f"{mkt}.{s['code']}"
                secids.append(s["secid"])
        if secids:
            r2 = self._get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={"secids": ",".join(secids), "fields": "f12,f13,f115", "fltt": "2"},
            )
            pe_map = {}
            for it in ((r2.json() or {}).get("data") or {}).get("diff") or []:
                try:
                    pe_map[f"{it.get('f13')}.{it.get('f12')}"] = float(it.get("f115"))
                except (TypeError, ValueError):
                    pass
            for s in stocks:
                if s["secid"] and s["secid"] in pe_map:
                    s["pe"] = pe_map[s["secid"]]
        out = {"stocks": stocks}
        out.update(calc_pe_stats(stocks))
        self.cache.set("hpe_v3", code, out)
        return out

    def fof_holdings(self, code):
        """FOF 的基金持仓（移动端持仓接口的 fundfofs，最新定期报告口径的全部持仓基金）：
        [{code,name,pct}]（TZJJDM/TZJJMC/ZJZBL，pct=占该 FOF 净值比例 %），无持仓返回 []。
        持仓季度更新，缓存 7 天。FOF 持仓分析页逐只调用。"""
        cached = self.cache.get("fofhold_v1", code, ttl=7 * 86400)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition",
            params={"FCODE": code, "deviceid": "1", "plat": "Iphone",
                    "product": "EFund", "version": "6.2.5"},
        )
        rows = ((r.json() or {}).get("Datas") or {}).get("fundfofs") or []
        out = []
        for it in rows:
            pct = None
            try:
                pct = float(it.get("ZJZBL"))
            except (TypeError, ValueError):
                pass
            out.append({"code": str(it.get("TZJJDM") or ""), "name": it.get("TZJJMC") or "",
                        "pct": pct})
        self.cache.set("fofhold_v1", code, out)
        return out

    def set_fof_held_map(self, held_map):
        """FOF 持仓分析完成时写入「份额代码 -> 被持 FOF 数」快照（分组查询总览表引用）。
        held_map 由 page_fof 从聚合结果展开：同一 FOF 持有 A/C 时两个份额代码各 +1。"""
        self.cache.set("fofagg_v1", "latest", {"held": held_map or {}})

    def get_fof_held_map(self):
        """最近一次 FOF 全市场分析的份额被持数快照 {份额代码: n}；未分析或超过 8 天
        （持仓缓存 7 天 + 一天余量）返回 {}，调用方显示 '--'。"""
        snap = self.cache.get("fofagg_v1", "latest", ttl=8 * 86400)
        return (snap or {}).get("held") or {}

    def search_funds(self, keyword):
        """按名称关键词搜索基金（天天基金搜索建议接口）。返回 [{code,name,type}]，按相关度排序"""
        cached = self.cache.get("search_v2", keyword, ttl=7 * 86400)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx",
            params={"m": 1, "key": keyword},
        )
        out, seen = [], set()
        for item in (r.json() or {}).get("Datas") or []:
            code = item.get("CODE") or ""
            if not re.fullmatch(r"\d{6}", code) or code in seen:
                continue
            base = item.get("FundBaseInfo") or {}
            seen.add(code)
            out.append({"code": code, "name": base.get("SHORTNAME") or item.get("NAME") or code,
                        "type": base.get("FTYPE") or ""})
            if len(out) >= 20:
                break
        # 接口排序不可靠（见 _search_rel_rank），按名称相关度重排；稳定排序保留组内原顺序
        out.sort(key=lambda m: _search_rel_rank(m["name"], keyword))
        self.cache.set("search_v2", keyword, out)
        return out

    def manager_dir(self):
        """基金经理目录（全量约 4300 人，来自经理排行榜数据接口）。
        每人含：姓名/公司/现任基金代码与名称列表/从业天数/在管总规模/现任最佳回报。
        没有按姓名搜索的接口，全量拉回来本地匹配；人员变动低频，缓存 7 天。"""
        cached = self.cache.get("mgrdir", "all", ttl=7 * 86400)
        if cached is not None:
            return cached
        rows, pi = [], 1
        while True:
            r = self._get(
                "https://fund.eastmoney.com/Data/FundDataPortfolio_Interface.aspx",
                params={"dt": 14, "mc": "returnjson", "ft": "all", "pn": 500,
                        "pi": pi, "sc": "abbname", "st": "asc"},
            )
            d = _parse_js_object(r.text)
            # 行结构：[经理ID, 姓名, 公司ID, 公司, 现任基金代码, 现任基金名称,
            #          从业天数, 最佳回报, 最佳回报基金代码, 最佳回报基金名称, 在管总规模, ...]
            rows += [
                {
                    "id": it[0],
                    "name": it[1],
                    "company": it[3],
                    "codes": [c for c in (it[4] or "").split(",") if c],
                    "names": [n for n in (it[5] or "").split(",") if n],
                    "days": it[6],
                    "scale": (it[10] or "").replace("亿元", "亿"),
                    "best_return": it[7],
                }
                for it in d.get("data") or []
                if len(it) >= 11
            ]
            pages = int(d.get("pages") or 1)
            if pi >= pages or pi >= 30:  # 防御：目录异常膨胀时最多 30 页
                break
            pi += 1
        self.cache.set("mgrdir", "all", rows)
        return rows

    def search_managers(self, name):
        """按姓名搜基金经理：精确 > 前缀 > 包含，同名多人全部返回（最多 20 位）"""
        name = (name or "").strip()
        if not name:
            return []
        rows = self.manager_dir()
        for pred in (lambda r: r["name"] == name,
                     lambda r: r["name"].startswith(name),
                     lambda r: name in r["name"]):
            out = [r for r in rows if pred(r)]
            if out:
                return out[:20]
        return []

    def manager_tenure(self, code):
        """基金经理任职记录（现任+离任）。该接口为附加信息，失败不致命。"""
        cached = self.cache.get("mgrtenure", code, ttl=12 * 3600)
        if cached is not None:
            return cached
        try:
            r = self._get(
                "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNMangerList",
                params={
                    "FCODE": code,
                    "deviceid": "1",
                    "plat": "Iphone",
                    "product": "EFund",
                    "version": "6.2.5",
                },
            )
            rows = []
            for item in (r.json() or {}).get("Datas") or []:
                days = str(item.get("DAYS", "--"))
                if days.endswith(".0"):
                    days = days[:-2]
                rows.append(
                    {
                        "managers": [m for m in (item.get("MGRNAME") or "").split(",") if m],
                        "start": item.get("FEMPDATE", "--"),
                        "end": item.get("LEMPDATE") if item.get("LEMPDATE") not in (None, "", "--") else "至今",
                        "days": days,
                        "return_pct": item.get("PENAVGROWTH", "--"),
                    }
                )
            self.cache.set("mgrtenure", code, rows)
            return rows
        except (FundApiError, ValueError):
            return []

    def periodic_notices(self, code):
        """定期报告公告列表（已按发布时间倒序），含季报/中期报告/年度报告"""
        cached = self.cache.get("notices", code, ttl=7 * 86400)
        if cached is not None:
            return cached
        r = self._get(
            "https://api.fund.eastmoney.com/f10/JJGG",
            params={"fundcode": code, "pageIndex": 1, "pageSize": 60, "type": 3},
        )
        items = (r.json() or {}).get("Data") or []
        self.cache.set("notices", code, items)
        return items

    def notice_detail(self, art_code):
        """公告详情：拿 PDF 附件地址"""
        cached = self.cache.get("nnotice", art_code, ttl=30 * 86400)
        if cached is not None:
            return cached
        r = self._get(
            "https://np-cnotice-fund.eastmoney.com/api/content/ann",
            params={"art_code": art_code, "client_source": "web", "page_index": 1},
        )
        data = (r.json() or {}).get("data") or {}
        self.cache.set("nnotice", art_code, data)
        return data

    def fund_universe(self):
        """天天基金全量基金代码表 [{code, name, type}]（fundcode_search.js，文件缓存 1 天）。
        type 为细分类型字符串，如 '混合型-偏股' / '股票型' / '指数型-股票' / '债券型-混合二级'。"""
        lst = os.path.join(self.cache.base, "fundcode_search.js")
        if not os.path.exists(lst) or time.time() - os.path.getmtime(lst) > 86400:
            open(lst, "wb").write(self._get("https://fund.eastmoney.com/js/fundcode_search.js").content)
        rows = json.loads(re.search(r"\[.*\]", open(lst, encoding="utf-8-sig").read(), re.S).group(0))
        return [{"code": c, "name": n, "type": t} for c, _, n, t, _ in rows]

    def holder_structure(self, code):
        """F10 持有人结构（FundArchivesDatas.aspx?type=cyrjg）：机构/个人/内部持有比例、总份额。
        「内部持有比例」= 基金管理公司从业人员持有本基金的比例（页面注释口径），
        比报告 PDF 的从业人员占比少两位小数精度，用于全市场初筛。
        返回 {date, internal, institution, individual, total}（最新一期在前取第一条），
        无数据（清盘/未披露）返回 {}。"""
        cached = self.cache.get("cyrjg_v1", code, ttl=7 * 86400)
        if cached is not None:
            return cached
        r = self._get(
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
            params={"type": "cyrjg", "code": code},
        )
        m = re.search(
            r"<tr><td>(\d{4}-\d{2}-\d{2})</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>"
            r"<td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>",
            r.text,
        )
        out = {}
        if m:
            date, inst, indiv, internal, total = m.groups()
            out = {
                "date": date,
                "institution": inst.strip(),
                "individual": indiv.strip(),
                "internal": internal.strip(),
                "total": total.strip(),
            }
        self.cache.set("cyrjg_v1", code, out)
        return out

    def download_pdf(self, url, filename):
        """下载公告 PDF 到缓存目录；自动处理 JS 反爬挑战。返回本地路径。"""
        dest = os.path.join(self.pdf_dir, filename)
        if os.path.exists(dest) and os.path.getsize(dest) > 10000:
            return dest
        r = self._get(url, timeout=self.timeout * 4)
        if looks_like_challenge(r.content):
            cookies = solve(r.content.decode("utf-8", "replace"))
            for k, v in cookies.items():
                self.session.cookies.set(k, v, domain="pdf.dfcfw.com")
            r = self._get(url, timeout=self.timeout * 4)
        if not r.content.startswith(b"%PDF"):
            raise FundApiError("下载的内容不是 PDF（反爬挑战未通过）")
        with open(dest, "wb") as f:
            f.write(r.content)
        return dest
