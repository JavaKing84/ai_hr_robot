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

**结果回传的顺序很重要**：必须先（或同时）消费结果队列，再等子进程退出。
``multiprocessing.Queue`` 由子进程内的 feeder 线程异步写管道，子进程退出清理时会
等 feeder 写完；主进程若先 ``process.join(timeout)`` 且一直不读队列，就形成互等——
子进程等主进程读、主进程等子进程退，最后被误判成「解析超时」并把结果丢掉。
2026-11-12 实测：一份 8.7MB / 3 页的 Boss 直聘 PDF 直接解析仅 0.94s，
旧实现却在 20s 后强杀子进程，导致 ``resume_text=0``、候选人姓名兜底成平台名。
"""

from __future__ import annotations

import asyncio
import inspect
import multiprocessing
import time
from dataclasses import dataclass
from queue import Empty as QueueEmpty
from typing import Any, Callable

#: 结果已取回后，允许子进程收尾的最大等待（秒）。
_RESULT_WAIT_SECONDS = 5.0
#: terminate 之后的宽限期（秒），超时再 kill。
_KILL_GRACE_SECONDS = 3.0
#: 主进程轮询结果队列的间隔（秒）；每次轮询同时检查超时与子进程是否已退出。
_POLL_INTERVAL_SECONDS = 0.05
#: 子进程已退出时，留给队列把残留数据刷完的时间（秒）。崩溃场景最多白等这么久。
_CHILD_EXIT_FLUSH_SECONDS = 0.5


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


def _stop_child(process: multiprocessing.Process) -> None:
    """尽可能让子进程停下来：先 terminate，宽限期内不退再 kill。

    只负责「停」，不负责判断失败原因，也不关闭结果队列；调用方按场景决定
    返回 ``timed_out`` 还是崩溃文本。
    """
    if not process.is_alive():
        return
    process.terminate()
    process.join(_KILL_GRACE_SECONDS)
    if process.is_alive():  # pragma: no cover - 极端情况下 terminate 也杀不掉
        process.kill()
        process.join(_KILL_GRACE_SECONDS)


def run_in_subprocess(
    func: Callable[..., Any],
    *args: Any,
    timeout: float = 60.0,
    **kwargs: Any,
) -> SubprocessResult:
    """在独立子进程中执行 ``func(*args, **kwargs)``，崩溃/超时都不外抛。

    ``func`` 需可 pickle（模块级函数或可序列化实例的绑定方法）；返回值同样要可 pickle。
    Windows 上使用 ``spawn``，子进程会重新 import 目标模块——因此 ``func`` 不能是闭包或 lambda。

    等待策略：以 ``timeout`` 为总上限，**持续读取结果队列**，同时观察子进程是否退出。
    这样既能让大结果顺利回传（避免 feeder 线程与 join 互等），又能在子进程段错误 /
    OOM 直接消失时快速失败，不必每次崩溃都白等满一个业务超时。
    """
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_child_entry,
        args=(func, args, kwargs, result_queue),
        daemon=True,
    )
    process.start()

    payload: tuple[str, Any] | None = None
    deadline = time.monotonic() + timeout
    timed_out = False

    # 主进程在这里持续消费队列：子进程 put() 之后才能顺利完成退出清理。
    # 旧实现是「先 join(timeout) 再 get()」，主进程等待期间无人读管道，
    # 大结果会卡住子进程的 feeder 线程，最终双双超时并被误杀。
    while True:
        try:
            payload = result_queue.get(timeout=_POLL_INTERVAL_SECONDS)
        except QueueEmpty:
            payload = None
        if payload is not None:
            break

        # 进程已经结束（正常返回 / 段错误 / OOM）：再给队列一小段时间刷残留数据；
        # 若确实没有结果，就按崩溃处理，不再空等到 deadline。
        if not process.is_alive():
            try:
                payload = result_queue.get(timeout=_CHILD_EXIT_FLUSH_SECONDS)
            except QueueEmpty:
                payload = None
            break

        if time.monotonic() >= deadline:
            timed_out = True
            break

    if timed_out:
        _stop_child(process)
        result_queue.close()
        result_queue.join_thread()
        return SubprocessResult(
            ok=False,
            timed_out=True,
            error=f"子进程执行超过 {timeout:g}s，已强制终止",
            exitcode=process.exitcode,
        )

    # 结果已经取回；正常情况子进程会立刻退出，但仍设上限防止非 daemon 线程拖住主进程。
    process.join(_RESULT_WAIT_SECONDS)
    if process.is_alive():
        _stop_child(process)
    result_queue.close()
    result_queue.join_thread()

    status = payload[0] if payload is not None else ""
    value = payload[1] if payload is not None else None

    if status == "ok" and process.exitcode in (0, None):
        return SubprocessResult(ok=True, value=value, exitcode=process.exitcode)

    # 子进程非 0 退出（段错误 / OOM / os._exit）优先报崩溃，队列里即使有半截结果也不采信。
    if process.exitcode not in (0, None):
        return SubprocessResult(
            ok=False,
            error="子进程异常退出（疑似解析器崩溃或内存耗尽）",
            exitcode=process.exitcode,
        )
    if status == "error":
        return SubprocessResult(ok=False, error=str(value), exitcode=process.exitcode)
    return SubprocessResult(
        ok=False,
        error="子进程未返回结果就退出（疑似解析器崩溃或内存耗尽）",
        exitcode=process.exitcode,
    )
