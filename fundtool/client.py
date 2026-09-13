# -*- coding: utf-8 -*-
"""天天基金（东方财富）数据客户端：基金基本信息、基金经理任职、定期报告公告、报告 PDF 下载、基金搜索"""
import os
import re
import time

import requests

from .jschallenge import looks_like_challenge, solve

# 注意：fundmobapi 的风控会拒绝带 AppleWebKit 字样的 UA，这里必须用简短 UA
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class FundApiError(Exception):
    pass


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
