# -*- coding: utf-8 -*-
"""限流熔断与查询中止的单元测试（纯本地，不联网）。

背景：批量查询被天天基金限流时，旧实现每个失败请求要退避 5/10/15/20 秒（一次 50 秒），
逐只跑会把几分钟拖成几小时。现在改为「短退避 + 连续失败熔断」，query_all 在熔断后
立即中止整批并把中止原因放进 errors[QUERY_ABORT_KEY]。
"""
import os
import sys
import time

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fundtool import FundApiError, JsonCache, RateLimitedError  # noqa: E402
from fundtool.client import EastFundClient  # noqa: E402
from ui.query import QUERY_ABORT_KEY, query_all  # noqa: E402


class _BoomSession:
    """所有请求都抛连接重置（模拟被数据源风控）"""

    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        raise requests.ConnectionError("Connection reset by peer")


class _OkResponse:
    def raise_for_status(self):
        return None


class _QueuedSession:
    """按队列返回成功/失败，用于验证"成功一次清零失败计数" """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        if not self.outcomes.pop(0):
            raise requests.ConnectionError("reset")
        return _OkResponse()


def _client(tmp_path, monkeypatch, **kw):
    # 跳过退避与节流的真实 sleep，测试秒级完成
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    return EastFundClient(JsonCache(str(tmp_path)), rate_limit=0, **kw)


def test_breaker_trips_then_blocks_requests(tmp_path, monkeypatch):
    """连续失败到阈值即熔断：先抛普通 FundApiError，第 N 次抛 RateLimitedError，
    之后冷却期内直接拒绝、不再打数据源"""
    c = _client(tmp_path, monkeypatch)
    c.session = _BoomSession()
    for _ in range(c.max_fail_streak - 1):
        with pytest.raises(FundApiError) as ei:
            c._get("https://example.invalid/x")
        assert not isinstance(ei.value, RateLimitedError), "未到阈值前应是普通请求失败"
    with pytest.raises(RateLimitedError):
        c._get("https://example.invalid/x")
    calls = c.session.calls
    with pytest.raises(RateLimitedError):
        c._get("https://example.invalid/x")
    assert c.session.calls == calls, "熔断冷却期内不应再请求数据源"


def test_success_resets_fail_streak(tmp_path, monkeypatch):
    """中间成功一次就把连续失败计数清零，孤立的单次失败不会误触发熔断"""
    c = _client(tmp_path, monkeypatch)
    # 每次 _get 内部最多尝试 3 次：3 个 False 才构成一次"请求失败"
    c.session = _QueuedSession([False] * 6 + [True] + [False] * 6)
    for _ in range(2):
        with pytest.raises(FundApiError):
            c._get("https://example.invalid/x")
    c._get("https://example.invalid/x")  # 成功一次 → 计数清零
    for _ in range(2):
        with pytest.raises(FundApiError) as ei:
            c._get("https://example.invalid/x")
        assert not isinstance(ei.value, RateLimitedError)


class _Status:
    def __init__(self):
        self.label = ""

    def update(self, label="", **kw):
        self.label = label


class _Progress:
    def progress(self, *a, **k):
        pass


def test_query_all_aborts_on_rate_limit(monkeypatch):
    """熔断后立即中止整批：保留已完成结果，剩余代码不再逐只空转，原因单独记账"""
    import ui.query as query_mod

    calls = []

    def fake_row(code, with_holding=True, benchmarks=None):
        calls.append(code)
        if len(calls) == 3:
            raise RateLimitedError("数据源连续 3 个请求失败（疑似限流）")
        return {"代码": code}

    monkeypatch.setattr(query_mod, "fund_overview_row", fake_row)
    results, errors = query_all(["000001", "000002", "000003", "000004"], False, _Status(), _Progress())
    assert list(results) == ["000001", "000002"], results
    assert QUERY_ABORT_KEY in errors, errors
    assert "000003" not in errors and "000004" not in errors, "中止后不应把剩余代码逐只标成失败"
    assert len(calls) == 3, calls
