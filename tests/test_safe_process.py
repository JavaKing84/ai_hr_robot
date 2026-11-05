"""子进程隔离执行器测试。

保护的核心是「主进程一定活着」：解析器崩溃、卡死、抛异常，调用方都只拿到一个
``SubprocessResult(ok=False)``，而不是跟着一起消失（对应 I-10 的单份失败隔离思路）。
"""

from __future__ import annotations

import os
import time

from core.safe_process import run_in_subprocess


def _returns_value(a: int, b: int = 0) -> int:
    return a + b


async def _async_returns_value(x: str) -> str:
    return x.upper()


def _raises_value_error() -> None:
    raise ValueError("解析器内部炸了")


def _hangs() -> None:
    time.sleep(10)


def _crashes_hard() -> None:
    """模拟 C 扩展段错误：子进程不留任何 Python 异常，直接消失。"""
    os._exit(11)


def test_returns_value_from_subprocess() -> None:
    result = run_in_subprocess(_returns_value, 2, b=3, timeout=30)
    assert result.ok is True
    assert result.value == 5


def test_awaits_coroutine_function() -> None:
    """解析入口是 async 的（save_and_parse），子进程里要自动 asyncio.run。"""
    result = run_in_subprocess(_async_returns_value, "abc", timeout=30)
    assert result.ok is True
    assert result.value == "ABC"


def test_exception_inside_child_is_returned_not_raised() -> None:
    result = run_in_subprocess(_raises_value_error, timeout=30)
    assert result.ok is False
    assert result.timed_out is False
    assert "ValueError" in result.error
    assert "解析器内部炸了" in result.reason


def test_timeout_kills_child_and_returns() -> None:
    started = time.monotonic()
    result = run_in_subprocess(_hangs, timeout=1)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.timed_out is True
    assert "超过" in result.reason
    assert elapsed < 9, f"应当按时终止子进程，实际耗时 {elapsed:.1f}s（子进程没被杀掉？）"


def test_child_hard_crash_reports_exitcode() -> None:
    """段错误 / os._exit 这类「没有 Python 异常」的崩溃，也必须变成普通返回值。"""
    result = run_in_subprocess(_crashes_hard, timeout=30)
    assert result.ok is False
    assert result.timed_out is False
    assert result.exitcode == 11
    assert "崩溃" in result.reason
