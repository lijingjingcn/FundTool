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
