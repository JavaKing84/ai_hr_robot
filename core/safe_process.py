"""把「可能把进程带崩」的第三方解析放进独立子进程执行。

**为什么需要这一层**：`pypdf` / `unstructured` / `pdfminer` 的解析核心是 C 扩展，
遇到截断、加密、字典异常或畸形 xref 的 PDF 时，常见表现不是抛 Python 异常，而是
**段错误 / 内存爆掉 / 卡死**。这类崩溃 `try/except` 兜不住——发生在主进程里，
整个调度轮次（甚至服务进程）就直接消失了，`dispatch_cycle` 的 I-10 隔离也救不了。

**做法**：在子进程里跑目标函数，主进程只等结果，三种失败都收敛成普通返回值：

- 超时 → ``terminate()``，仍在则 ``kill()``，返回 ``timed_out=True``；
- 子进程非 0 退出（段错误 / OOM / 被 terminate）→ 返回 ``exitcode`` 与错误文本；
- 子进程正常返回但途中抛了异常 → 返回 ``error`` 文本。

**任何情况下都不把异常抛给调用方**：降级方式（空文本 / 跳过 / 告警）由 service 层决定，
本模块只负责「主进程一定活着」。
"""

from __future__ import annotations

import asyncio
import inspect
import multiprocessing
from dataclasses import dataclass
from queue import Empty as QueueEmpty
from typing import Any, Callable

#: 子进程结果回传等待上限（秒）。仅用于取回已经算完的结果，不是业务超时。
_RESULT_WAIT_SECONDS = 5.0
#: terminate 之后的宽限期（秒），超时再 kill。
_KILL_GRACE_SECONDS = 3.0


@dataclass(frozen=True)
class SubprocessResult:
    """子进程执行结果。``ok=False`` 时看 ``error`` / ``timed_out`` 判断失败形态。"""

    ok: bool
    value: Any = None
    error: str = ""
    timed_out: bool = False
    exitcode: int | None = None

    @property
    def reason(self) -> str:
        """给日志/告警用的一句话失败原因（成功时为空串）。"""
        if self.ok:
            return ""
        if self.timed_out:
            return self.error or "子进程执行超时"
        if self.exitcode not in (None, 0):
            return f"{self.error}（exitcode={self.exitcode}）"
        return self.error or "子进程执行失败"


def _child_entry(func: Callable[..., Any], args: tuple, kwargs: dict, result_queue: Any) -> None:
    """子进程入口：执行目标函数（协程自动跑完）并把结果放回队列。

    这里捕获 ``BaseException``：子进程里的 ``SystemExit`` / ``KeyboardInterrupt``
    同样要当成「这次解析失败」回传，而不是让它变成一个没人看懂的退出码。
    """
    try:
        value = func(*args, **kwargs)
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        result_queue.put(("ok", value))
    except BaseException as exc:  # noqa: BLE001 - 子进程必须兜住一切并如实回传
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def run_in_subprocess(
    func: Callable[..., Any],
    *args: Any,
    timeout: float = 60.0,
    **kwargs: Any,
) -> SubprocessResult:
    """在独立子进程中执行 ``func(*args, **kwargs)``，崩溃/超时都不外抛。

    ``func`` 需可 pickle（模块级函数或可序列化实例的绑定方法）；返回值同样要可 pickle。
    Windows 上使用 ``spawn``，子进程会重新 import 目标模块——因此 ``func`` 不能是闭包或 lambda。
    """
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_child_entry,
        args=(func, args, kwargs, result_queue),
        daemon=True,
    )
    process.start()
    process.join(timeout)

    if process.is_alive():
        process.terminate()
        process.join(_KILL_GRACE_SECONDS)
        if process.is_alive():  # pragma: no cover - 极端情况下 terminate 也杀不掉
            process.kill()
            process.join(_KILL_GRACE_SECONDS)
        return SubprocessResult(
            ok=False,
            timed_out=True,
            error=f"子进程执行超过 {timeout:g}s，已强制终止",
            exitcode=process.exitcode,
        )

    # 子进程正常跑完一定是 exitcode=0；非 0 说明它被信号打死 / os._exit / 内存耗尽，
    # 此时队列里不可能有结果，不必再等（否则每次崩溃都要白等一个结果超时）。
    if process.exitcode not in (0, None):
        result_queue.close()
        result_queue.join_thread()
        return SubprocessResult(
            ok=False,
            error="子进程异常退出（疑似解析器崩溃或内存耗尽）",
            exitcode=process.exitcode,
        )

    try:
        status, payload = result_queue.get(timeout=_RESULT_WAIT_SECONDS)
    except QueueEmpty:
        # 子进程没留下结果就没了：段错误 / OOM / 被系统杀掉都是这个形态
        return SubprocessResult(
            ok=False,
            error="子进程未返回结果就退出（疑似解析器崩溃或内存耗尽）",
            exitcode=process.exitcode,
        )
    finally:
        result_queue.close()
        result_queue.join_thread()

    if status == "ok":
        return SubprocessResult(ok=True, value=payload, exitcode=process.exitcode)
    return SubprocessResult(ok=False, error=str(payload), exitcode=process.exitcode)
