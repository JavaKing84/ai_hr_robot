# PDF 解析子进程隔离方案（可行性说明）

> 文档性质：**后续演进方向**，非本轮实施方案
> 关联限制：[known-limitations.md](./known-limitations.md)（真实损坏 PDF 触发原生崩溃）
> 状态：**不在本轮实施**——仅记录可行性，避免引入超出当前范围的新依赖或架构复杂度

---

## 1. 为什么 Python 的 `try / except` 无法捕获原生崩溃

1. `unstructured` 解析 PDF 时，底层调用的是 **C/C++ 原生库**（如 `pdfminer.six` 的 C 扩展、`libmagic`、或 PDF 解析器内部的 C 实现）。
2. 当遇到损坏 / 恶意构造的字节流时，原生代码可能出现**内存越界、空指针解引用**等未定义行为，操作系统以 **`SIGSEGV`（信号 11）或 `SIGABRT`（信号 6）** 直接终止进程。
3. 这些信号**不经过 Python 的异常机制**：
   - 它们不是 `Exception` / `BaseException` 的实例，所以 `except` 匹配不到；
   - 即使进程注册了信号处理器，原生库的崩溃往往发生在不可恢复状态，处理器也只能做"记录后退出"，无法"恢复后继续"。
4. 结论：**崩溃点的防护必须在"进程边界"之外**——也就是用一个独立的进程去承载不可信的解析，主进程只负责"等结果"和"超时判定"。

## 2. 轻量级子进程隔离方案

核心思路：**把不可信的 PDF 解析放到一个独立子进程里，主进程通过超时来控制它；子进程崩溃 = 超时 = 降级。**

### 2.1 调用骨架（示意）

```python
import multiprocessing as mp
from dataclasses import dataclass


@dataclass
class _ParseResult:
    text: str
    warnings: list[str]


def _parse_in_subprocess(payload: bytes, q: "mp.Queue") -> None:
    """在子进程内执行原生解析；崩溃则整进程消失，主进程靠超时感知。"""
    try:
        from client.resume_parser import _try_unstructured  # 仅子进程内导入风险库
        warnings: list[str] = []
        text = _try_unstructured(payload, warnings)  # 可能 SIGSEGV/SIGABRT
        q.put(_ParseResult(text or "", warnings))
    except Exception as exc:  # 仅捕获 Python 层异常
        q.put(_ParseResult("", [f"subproc-python-error: {exc}"]))


def safe_parse(payload: bytes, timeout: float = 30.0) -> _ParseResult:
    q: "mp.Queue" = mp.Queue(maxsize=1)
    proc = mp.Process(target=_parse_in_subprocess, args=(payload, q), daemon=True)
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.kill()                      # 超时：强制结束
        return _ParseResult("", ["parse.timeout: 子进程超时，已降级"])
    if not q.empty():
        return q.get()                   # 正常 / Python 层异常结果
    # 队列空 + 进程已死 → 子进程被信号杀死（原生崩溃）
    return _ParseResult("", ["parse.crash: 子进程原生崩溃，已降级"])
```

### 2.2 主流程如何接入

- `ResumeParser.parse_path / parse_bytes` 在调用主引擎（`unstructured`）前，改为走 `safe_parse`；
- `safe_parse` 返回空文本 + `warnings` 时，原有降级链（`pypdf` / `python-docx`）照常接管 —— **业务层降级逻辑完全复用，不重写**；
- 主进程（承载 `run_daily_cycle` 的 worker）永远不会因单个坏文件而死，本轮其余邮件照常处理。

### 2.3 方案要点

| 维度 | 说明 |
|---|---|
| 隔离粒度 | 每次解析一个子进程（或进程池复用，视吞吐而定） |
| 超时 | 主进程 `join(timeout)` 控制；超时即 `kill()`，不等原生库自愈 |
| 降级路径 | 子进程崩溃 → 返回空文本 + 告警 warning → 业务层落到 `pypdf` 等安全引擎 |
| 资源 | `daemon=True` 防止僵尸；必要时设置子进程内存上限（容器 cgroup / `resource` 模块） |
| 可观测 | `parse.crash` / `parse.timeout` warning 进日志，运维可直接检索 |

## 3. 为什么**不在本轮实施**

1. **超出当前授权范围**：本轮优化框架（§4-A 子任务 1/2/3 + 任务 3/4）聚焦工程稳定性、文档与测试，不含架构重构；子进程隔离是架构级改动。
2. **引入新复杂度 / 依赖**：`multiprocessing` 本身是标准库，但跨进程传递（`Queue` / 序列化）会增加调试面与平台差异（Windows 启动方式、spawn vs fork），在 Windows 上尤其需要谨慎。
3. **收益与风险不匹配当前阶段**：当前已知限制已通过"monkeypatch 验证降级 + 文档标注停机项"得到控制；在还没遇到真实坏文件致瘫的生产事故前，优先保持稳定，不贸然加层。
4. **护栏约束**：AGENT.md 要求变更可控、不引入不必要的依赖与架构复杂度；此方案属于"演进方向"而非"修复项"。

## 4. 后续若决定实施的触发条件

- 生产环境真实出现因坏 PDF 致 worker 崩溃、影响整轮批处理；
- 或风控要求对来源不可信 PDF 做强制隔离（合规 / 安全要求）；
- 实施前需补充：Windows 平台 `multiprocessing` 启动方式验证、子进程内存/超时压测、崩溃计数与告警阈值。

---

## 5. 小结

`try/except` 防不住原生崩溃，这是事实而非疏漏。**正确且克制的做法是：把不可信解析关进独立进程，用超时判定代替异常捕获，崩溃即降级**——但这一步属于架构演进，本轮仅在文档中记录可行性与约束，不做实现。
