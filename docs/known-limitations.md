# 已知限制（Known Limitations）

> 文档性质：**已知限制 / 停机项**记录
> 关联护栏：AGENT.md §3.1 #1（风险区：损坏 PDF 触发 unstructured 原生库崩溃）
> 关联演进方案：[pdf-isolation-plan.md](./pdf-isolation-plan.md)

> **2026-11-05 更新（修复状态）**
> - **PDF 崩溃 → 已修复**：按本文件第 3 节「后续彻底解决方向」落地了子进程隔离，
>   入口 `core/safe_process.py::run_in_subprocess`，调用点 `service/ingestion.py::_parse_resume_safely`。
> - **相对月份盲区（E-01：「下个月 X 号」解析不出来）→ 已修复**：
>   `core/timeparse.py` 新增 `_NEXT_MONTH_DAY_RE` 独立拦截，先红后绿的测试在
>   `tests/test_timeparse.py`。
> - 详见新增的 [§5 修复记录](#5-修复记录2026-11-05)。

---

## 1. ~~限制描述~~（已修复，见 §5.1）

**真实损坏 / 恶意构造的 PDF 可能触发 `unstructured` 底层原生库（C/C++ 实现，如 PDFMiner / libmagic 等）的进程级崩溃，表现为 `SIGSEGV`（段错误）或 `SIGABRT`（中止信号）。**

- 该崩溃发生在 Python 解释器之外的原生代码层；
- 它不是 Python 异常（`Exception` / `BaseException` 的子类型），因此**无法被 `try / except` 捕获**；
- 信号到达时操作系统会直接终止整个进程，后续的 Python 代码（包括 `finally`、上下文管理器清理、`atexit` 钩子）都**不会执行**；
- 后果：当前进程（可能是承载整轮 `run_daily_cycle` 的 worker）被杀死，本轮正在处理的其余邮件/候选人随之中断。

## 2. 当前测试策略

由于上述原因，**我们无法用真实崩溃文件来"验证"业务层的降级逻辑**——一旦投喂真实崩溃文件，崩溃发生在 Python 之外，测试进程本身就会被杀，断言根本没有机会运行。

因此本项目的边界测试采用以下**安全的等价验证策略**：

- 使用 `monkeypatch` 在 **Python 层**模拟主解析引擎（`unstructured`）失败（返回一个空文本 + 注入 `warnings`），而非投喂真实损坏字节；
- 验证的是**业务层**在"主引擎返回失败"这一确定条件下，能否正确：
  - 降级到后续引擎（`pypdf` / `python-docx`）；
  - 保留 `warnings` 留痕（供运维检索）；
  - 不抛出未捕获异常、不中断批处理。
- 对应测试：`tests/test_edge_cases.py :: test_unstructured_failure_falls_back_gracefully`。

> 该策略只覆盖"Python 层可观察的失败"。**真实原生崩溃场景不在自动化测试范围内**——它超出了 `try/except` 的能力边界，必须由进程级隔离（见下一节）来兜底。

## 3. 状态与处理原则

| 项 | 说明 |
|---|---|
| 标记 | **已知限制 / 停机项（§3.1 #1）** |
| 是否在本轮解决 | ~~**否**。本轮仅记录，不引入新依赖或架构改动~~ → **已解决（2026-11-05）**，见 §5.1 |
| 为何不临时绕过 | 在 `client/resume_parser.py` 里加 `try/except` 是**无效**的（捕获不到原生信号），属于"看起来做了实际没用"的伪防护 |
| 后续彻底解决方向 | **子进程隔离**：把 PDF 解析调用放入独立进程，主进程设超时，子进程崩溃则主进程捕获并降级（详见 [pdf-isolation-plan.md](./pdf-isolation-plan.md)） |
| 运维缓解 | 解析 worker 独立部署 / 崩溃后由调度器自动重启；对来源不明 PDF 先做文件头 / 大小 / 类型白名单粗筛（业务层可做，不触碰风险区） |

---

## 4. 小结

这处限制的本质是：**第三方原生库的稳定性不属于本项目的代码可控范围**。我们不是在"逃避"，而是清醒地把"无法在 Python 层防御"的事实标注为停机项，并用"monkeypatch 验证降级 + 子进程隔离作为后续演进"的两条腿走路——既不让测试假装能覆盖崩溃，也不让崩溃成为无人认领的隐患。

---

## 5. 修复记录（2026-11-05）

### 5.1 损坏 PDF 崩溃 → 子进程隔离（已修复）

| 项 | 内容 |
|---|---|
| 方案 | 解析调用移入独立子进程执行，主进程只等结果；超时 `terminate()`、非 0 退出/信号致死一律收敛成普通返回值 |
| 实现 | 新增 `core/safe_process.py`（`run_in_subprocess` / `SubprocessResult`）；调用点 `service/ingestion.py::_parse_resume_safely` 包裹 `parser.save_and_parse` |
| 原 `client/resume_parser.py` | **未改动**（§3.1 #1 的降级链、`warnings` 留痕、异常不抛错全部原样保留） |
| 降级行为 | 子进程崩溃/超时 → 返回空文本 `ParsedResume`（`engine="subprocess-failed"`）+ `warnings` 记录原因 → 后续按「未归位」留档并告警，**本轮其他简历照常处理** |
| 超时 | `service/ingestion.py::_PARSE_TIMEOUT_SECONDS = 60`（单份简历解析上限，非业务超时） |
| 测试 | `tests/test_safe_process.py`（正常返回 / 协程 / 子进程抛异常 / 超时被杀 / 硬崩溃 exitcode=11）；`tests/test_ingestion.py::test_crashing_parser_degrades_to_warnings`、`::test_ingest_survives_parser_crash` |
| 代价 | 每份简历多一次进程启动（Windows `spawn` 约 0.3~0.8s）；全量测试耗时从 ~45s 升到 ~85s |

> 仍未覆盖的部分：子进程本身被 OOM killer 干掉时，日志只能看到「未返回结果就退出」，
> 具体是哪个 C 扩展炸的仍要人工复现——这属于第三方库的调试范畴，不在本项目可控范围。

### 5.2 相对月份盲区 E-01（已修复）

| 项 | 内容 |
|---|---|
| 现象 | 主管说「下个月 5 号」时，`_parse_date` 无任何分支命中 → 整句退化为「只给了时间」或直接返回 `None`，等于把主管已经给出的时间丢掉（与 I-01 冲突） |
| 修复 | `core/timeparse.py` 新增 `_NEXT_MONTH_DAY_RE`，并在 `_parse_date` **开头**加一个独立 `if` 拦截；原有 ISO / 绝对月日 / 相对日 / 周几 分支**逐行未动**，函数签名未变 |
| 支持表达 | `下个月5号`、`下月5号`、`下个月15号下午2点`、`下个月1号上午九点半`、`下个月28日 10:00` |
| 测试 | `tests/test_timeparse.py::test_next_month_day_parsed`（5 个参数化）、`::test_next_month_day_does_not_steal_other_expressions`（回归保护）；**先红后绿**：修复前 6 条失败，修复后全绿 |
| 残留限制 | 「下个月 30 号」出现在只有 28/29 天的月份时，`_parse_date` 返回 `None`，流程退化为既有的「只给时间」分支（与「2月30号」的历史行为一致）。彻底修复需要改 `parse_chinese_datetime` 主流程，属重构，本轮未做——**运营侧表现为：这类简历/指令仍会走「向主管追问」或留待人工确认** |
