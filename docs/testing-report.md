# 测试体系报告（任务 4：边界场景与异常输入补强）

> 定位：本系统不是「跑通流程的脚本」，而是一套**用边界测试锁死异常输入**的鲁棒工具。  
> 任何一类意外输入（缺时间、缺姓名、重复投递、附件损坏）若被错误实现，都会有对应测试变红。  
> 当前测试总数：**154 个用例，全绿**（`147` 既有 + `7` 本次新增边界测试）。



---

## 1. 测试覆盖场景矩阵

> 列说明：**场景** → **测试文件:用例** → **守护的业务不变量**（即「这条测试在锁死什么」）→ **若无此测试的后果**。

### 1.1 本次重点审查的 4 类边界（任务 4 要求）

| 场景              | 测试文件:用例                                                                                                                                             | 守护的业务不变量                                                                                    | 若无此测试的后果                 |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------ |
| 含糊指令·**缺面试时间**  | `tests/test_edge_cases.py::test_supervisor_missing_time_does_not_send_notice`                                                                       | 主管只给姓名不给时间 → 机器人必须回飞书追问，**绝不猜一个时间发错面试邮件**（不变量 I-05 精神：不猜不发）                                 | 机器人可能把时间填错，向候选人发出错误面试通知  |
| 含糊指令·**缺候选人姓名** | `tests/test_edge_cases.py::test_supervisor_missing_name_does_not_send_notice`                                                                       | 主管只给时间不给姓名 → 名单必须来自本轮已发候选人，**绝不向未知人选发邮件**                                                   | 机器人可能把时间套到错误的人身上         |
| 重复简历及附件         | `tests/test_edge_cases.py::test_duplicate_resume_not_ingested_twice`                                                                                | 相同邮箱+手机号二次投递 → `is_duplicate=True`、数据库只留 1 条、追加 `candidate.duplicate` 事件（不变量：不得产生「双份」与重复通知） | 同一候选人被重复入库，主管端出现双份、重复发通知 |
| 附件损坏 / 无法解析     | `tests/test_edge_cases.py::test_empty_resume_rejected`、`test_missing_file_rejected`、`test_unstructured_failure_falls_back_gracefully`               | 解析层任何引擎失败都必须**降级不崩溃、留 warnings 留痕**（不变量 I-10：单点失败不阻断批处理）；风险区 `unstructured` 绝不被真实损坏文件触发原生崩溃 | 一份坏简历拖垮整轮收件，或直接进程崩溃      |
| 乱序信息（时间在名字前）    | `tests/test_edge_cases.py::test_out_of_order_schedule_still_notifies`（正向对照）+ `scripts/eval_extraction.py` 2 条乱序样本 + `tests/test_extraction_eval.py` | 顺序颠倒（「下周三张明来面试」）不应误判为含糊指令，仍正确抽取并发送                                                          | 正常乱序消息被当成异常丢弃，漏发通知       |

### 1.2 既有测试已覆盖的相关不变量（节选，证明体系早已非「脚本」）

| 场景                         | 测试文件:用例                                                                                                                                 | 守护的业务不变量                                       |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| 抽取·LLM 不可用/失败/无效 schema 降级 | `tests/test_extraction.py::test_llm_failure_falls_back_to_rules`、`test_llm_invalid_schema_falls_back`                                   | 抽取永远回落到确定性规则路径，业务层不感知来源                        |
| 抽取·姓名接地（I-05，防误认人名）        | `tests/test_extraction_eval.py::test_names_are_grounded_to_roster`、`tests/test_extraction.py::test_llm_names_are_grounded_to_roster`    | 出现在名单里的姓名必须全部来自名单，绝不把「12 份」「小李」当人名             |
| 名单精确匹配                     | `tests/test_extraction_eval.py::test_interview_names_exact_match`                                                                       | 规则路径抽出与人工标注完全一致的候选人集合                          |
| 回信意图识别                     | `tests/test_extraction_eval.py::test_reply_intent_exact_match`                                                                          | confirm / reschedule / decline 必须与人工标注一致       |
| 幂等防重·面试通知                  | `tests/test_stability.py::test_interview_notice_same_message_id_sends_once`                                                             | 同一 Message-ID 重发两次，候选人只收到一封                    |
| 幂等补发·失败重发不重复               | `tests/test_stability.py::test_failed_notice_retried_without_duplicate`、`test_feishu_failed_message_retried_once`                       | 失败只补发一次，绝不连发多封                                 |
| 异常隔离·单步失败不阻断               | `tests/test_stability.py::test_run_daily_cycle_continues_when_step_fails`、`test_single_position_failure_does_not_block_others`          | I-10：任一步/任一职位失败，其余照常、闭环正常收尾                    |
| 职位匹配失败降级                   | `tests/test_ingestion.py::test_unmatched_resume_stored_and_feishu_alerted`、`test_no_active_positions_still_stored_without_feishu_alert` | 无法归位不抛 400、停留 `received` + 飞书告警（不破坏 10 状态枚举契约） |
| 端到端闭环（正常路径对照）              | `tests/test_workflow.py`（5 个用例）                                                                                                         | 收简历→筛选→飞书发主管→主管回复→面试邮件→改期/放弃 主链路正确             |
| 状态机枚举冻结（附录 A）              | 全部用例隐式守护                                                                                                                                | 不变量：10 个 `CandidateStatus` 取值永久不变；任何新增都需走评审    |

---

## 2. 本次新增的边界测试（任务 4 补强）详解

新增文件：`tests/test_edge_cases.py`（**7 个用例，全部离线、确定性、独立**）。

| 用例                                                  | 类型      | 锁死的点                                                                                       |
| --------------------------------------------------- | ------- | ------------------------------------------------------------------------------------------ |
| `test_supervisor_missing_time_does_not_send_notice` | 含糊·缺时间  | feedback 层 `missing_time` 分支：发飞书追问，**outbox 中无任何「面试通知」邮件**                                 |
| `test_supervisor_missing_name_does_not_send_notice` | 含糊·缺姓名  | feedback 层 `unparsed` 分支：发格式说明，**outbox 中无任何「面试通知」邮件**                                     |
| `test_duplicate_resume_not_ingested_twice`          | 重复简历    | 二次投递 `is_duplicate=True`、数据库仅 1 条、追加 `candidate.duplicate` 事件                              |
| `test_empty_resume_rejected`                        | 附件边界    | 空简历内容 → 显式 `ValidationFailed`，而非静默入库                                                       |
| `test_missing_file_rejected`                        | 附件边界    | 文件不存在 → `ValidationFailed`，而非崩溃                                                            |
| `test_unstructured_failure_falls_back_gracefully`   | 附件损坏降级  | 用 monkeypatch 模拟 `unstructured` 不可用（**绝不投真实损坏文件**，规避原生崩溃风险区），断言降级到后续引擎、文本为空但 `warnings` 留痕 |
| `test_out_of_order_schedule_still_notifies`         | 乱序·正向对照 | 「下周三张明来面试」仍正确抽取并发送 1 封面试通知                                                                 |

### 关于「附件损坏」测试的安全约定（重要）

真实损坏 PDF 会触发 `unstructured` 原生库的**进程级崩溃（不可捕获）**——这是本项目的 🔴 停机项，测试**绝不**直接投喂损坏文件。新测试改用 monkeypatch 模拟 `unstructured` 失败，从而只验证「代码已对失败做降级留痕」这一业务逻辑，既锁死了不变量，又完全避开了风险区。

---

## 3. 为什么这些测试证明「不是脚本，是工具」

1. **异常分支有断言，而非只跑 happy path。** 脚本通常只验证「正常输入能跑通」；本系统对每一类异常（缺时间/缺姓名/重复/损坏）都有专门用例，且断言的是**不该发生的事**（如「outbox 中不得出现面试通知邮件」）。
2. **降级路径被显式锁死。** LLM 不可用、解析引擎失败、职位无法归位、单封邮件崩溃——这些「真实世界必然发生」的事，都被写成不变量（I-10：单点失败不阻断全局）。
3. **确定性 + 全离线。** 所有外部依赖（飞书/mailbox/LLM）走 mock，时间固定，用例可重复、可进 CI，不会因网络或「今天周几」而飘红。
4. **红线由测试守卫。** 10 状态枚举、姓名接地 I-05 等冻结契约，一旦被改动就有测试变红，从机制上阻止「顺手改个状态名」破坏历史数据。

---

## 4. 验收命令（供评委截图）

> 建议在项目根目录 `E:\project\hr-ai-robot` 执行。`.venv` 为本地虚拟环境；  
> `--basetemp=var/pytest-tmp` 把临时目录固定到项目内 `var/`，避免落到系统 `Temp` 触发安全软件的「批量删除」提示（即便提示，那也只是 pytest 自己的临时文件，可放心放行）。

**① 全量测试（带 `-v` 逐用例展示，适合截图）：**

```bash
./.venv/Scripts/python.exe -m pytest -v --basetemp=var/pytest-tmp
```

**② 只看本次新增的边界测试（7 个）：**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_edge_cases.py -v --basetemp=var/pytest-tmp
```

**③ 简洁模式（只看总数与失败）：**

```bash
./.venv/Scripts/python.exe -m pytest -q --basetemp=var/pytest-tmp
```

预期结果：`154 passed, 0 failed, 0 error`（或在 `-v` 下逐条列出 `PASSED`）。

> 补充：若不使用 `--basetemp`，pytest 默认也会在结束时清理临时目录并可能弹出删除确认，属正常行为，不影响项目源码。
