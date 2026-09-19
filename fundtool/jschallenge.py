# -*- coding: utf-8 -*-
"""pdf.dfcfw.com 的 JS 反爬挑战求解：把挑战脚本交给 Node 执行，收集它设置的 cookie"""
import os
import shutil
import subprocess

_SOLVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solve_challenge.js")

# PATH 解析不到 node 时的常见安装位置兜底（如代理/服务进程的 PATH 被破坏时）
_NODE_FALLBACKS = (
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
)


def _find_node():
    p = shutil.which("node")
    if p:
        return p
    for c in _NODE_FALLBACKS:
        if os.path.exists(c):
            return c
    return None


def looks_like_challenge(content: bytes) -> bool:
    head = content[:1024].lstrip()
    return head.startswith(b"<script") and b"cookie" in head


def solve(script_text: str) -> dict:
    """返回 {cookie名: cookie值}"""
    node = _find_node()
    if not node:
        raise RuntimeError(
            "未找到 Node.js（下载报告 PDF 需用它求解反爬挑战）。请安装：https://nodejs.org/"
        )
    p = subprocess.run(
        [node, _SOLVER],
        input=script_text.encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    if p.returncode != 0:
        raise RuntimeError("挑战脚本执行失败: " + p.stderr.decode("utf-8", "replace")[:300])
    cookies = {}
    for part in p.stdout.decode("utf-8", "replace").split(";;"):
        part = part.strip().strip(";").strip()
        if "=" in part:
            name, _, value = part.partition("=")
            if name.strip():
                cookies[name.strip()] = value.strip()
    return cookies
