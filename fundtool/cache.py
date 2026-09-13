# -*- coding: utf-8 -*-
"""简单的 JSON 文件缓存，带 TTL"""
import json
import os
import time


class JsonCache:
    def __init__(self, base_dir=".cache"):
        self.base = os.path.abspath(base_dir)
        os.makedirs(self.base, exist_ok=True)

    def _path(self, ns, key):
        d = os.path.join(self.base, ns)
        os.makedirs(d, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(key))
        return os.path.join(d, safe + ".json")

    def get(self, ns, key, ttl=None):
        """ttl=None 表示永不过期，否则为秒数；未命中返回 None"""
        p = self._path(ns, key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return None
        if ttl is not None and time.time() - rec.get("ts", 0) > ttl:
            return None
        return rec.get("value")

    def set(self, ns, key, value):
        with open(self._path(ns, key), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "value": value}, f, ensure_ascii=False)
