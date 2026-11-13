# 07 · 测试、缺口与风险

---

## 1. 测试执行结果（真实运行，不美化）

### 1.1 命令

```bash
cd D:\!pythonproject\hr_ai_robot
TEST_PG_AUTOSTART=0 .venv/Scripts/python.exe -m pytest -q
```

- `TEST_PG_AUTOSTART=0`：禁止 conftest 自动 `docker compose up postgres`（避免动用户已运行的容器）
- 数据库：沿用 `hr_robot_test`（由 `.env` 的 `DATABASE_URL` 派生）。conftest 会**每个用例前 `TRUNCATE ... RESTART IDENTITY CASCADE`**（`tests/conftest.py:203-222`），因此**该测试库原有数据已被清空**。
- 先试过用独立库 `hr_robot_test_intel`（避免触碰既有测试库），失败：
  ```
  InsufficientPrivilegeError: permission denied to create database
  目标库：postgresql+asyncpg://hr:hr@localhost:5432/hr_robot_test_intel
  ```
  原因：`hr` 账号无 CREATEDB 权限（本机业务库实际用 `postgres` 超级用户连接，见 `05_CONFIG_AND_DEPLOYMENT.md` §4.4）。故改用已存在的 `hr_robot_test`。

### 1.2 结果（2026-11-14T00:51，耗时 1 分 16 秒）

```
........................................................................ [ 37%]
........................................................................ [ 75%]
..............................................                           [100%]
```

- **190 passed / 0 failed / 0 error**，进程退出码 0。
- 与 `README.md:441`（"190 个用例"）一致；与 `docs/quality/testing-report.md:5,94`（"154"）**不一致**——文档落后。
- 末尾出现一条 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":89,...Temp\\pytest-of-26808\\garbage-...}`：这是 pytest 清理临时目录被本地安全钩子拦截的提示，**不影响用例结果**（退出码 0 可证）。

### 1.3 ⚠️ 当前无法复跑（环境问题，非代码问题）

本次扫描期间，同一路径解释器行为发生了变化：

| 时刻 | `python -V` | 结果 |
|---|---|---|
| 00:50 | Python 3.13.14 | 依赖导入正常，pytest 跑完 190 例 |
| 00:57 起 | Python 3.14.7 | `import asyncpg` → `No module named 'asyncpg.protocol.protocol'`；`import pydantic_core` → `cannot import name 'ArgsKwargs'` |

实测证据：
```
.venv/Lib/site-packages/asyncpg/protocol/protocol.cp313-win_amd64.pyd
.venv/Lib/site-packages/greenlet/_greenlet.cp313-win_amd64.pyd
.venv/pyvenv.cfg → version = 3.14.7 , home = D:\python
```
即：**venv 里的二进制扩展是 cp313 编译的，而解释器现在报 3.14.7**。当前状态下 `pytest --collect-only` 在 `import pydantic_core` 阶段就崩，应用也无法启动。

结论与处置建议（未执行，需用户确认）：
1. 用 Python **3.13** 重建 `.venv`（与本机 `.pyd` 版本匹配），或
2. 保持 3.14 解释器，重装全部依赖（`pip install -r requirements.txt` 会取下 cp314 轮子）。

> 本条是**环境事实**，不写进"代码缺陷"。190 个用例的通过结论以 00:51 那次真实运行为准。

---

## 2. 测试分布与覆盖范围（190 个）

| 文件 | 用例 | 覆盖对象 |
|---|---|---|
| `test_extraction_eval.py` | 52 | 抽取质量评估：姓名接地、名单精确匹配、回信意图与人工标注比对（离线样本集） |
| `test_timeparse.py` | 28 | 中文时间解析（「下周三下午两点」、跨月、越界日期、时间在前在后） |
| `test_extraction.py` | 16 | 抽取管道：LLM 失败/无效 schema 降级、姓名接地、规则与模型字段合并 |
| `test_stability.py` | 12 | 幂等（同 Message-ID 只发一封）、失败补发不重复、单步失败不阻断、单职位失败隔离 |
| `test_semantic_scoring.py` | 9 | 语义分融合、影子模式、深度解析归一化、硬条件失败跳过 LLM |
| `test_feishu_events.py` | 9 | 事件归一化（text/post/卡片/@提及/未知事件） |
| `test_screening.py` | 8 | 硬性淘汰、加权打分、阈值、unknown 字段不误杀 |
| `test_email_threading.py` | 8 | In-Reply-To/References/token/发件人四级兜底、平台发件人、带附件例外 |
| `test_edge_cases.py` | 7 | 缺时间/缺姓名不群发、重复投递、空简历、文件不存在、解析引擎降级、乱序信息 |
| `test_api_misc.py` | 7 | `/health`、错误信封、仓库无前端文件 |
| `test_scheduler.py` | 6 | 调度器注册与 cron/interval 规则 |
| `test_workflow.py` | 5 | 端到端：收→筛→发主管→回执→通知→改期/放弃 |
| `test_interview_confirm_gate.py` | 5 | 面试通知确认闸门：挂起/确认/取消/序号选择 |
| `test_llm_providers.py` | 5 | `LLM_PROVIDERS` 解析、降级链、全挂抛 `LLMUnavailable`、`safe_*` 返回 None、reasoning 里取 JSON（全部 monkeypatch，**不联网**） |
| `test_safe_process.py` | 5 | 子进程崩溃/超时/非 0 退出的收敛 |
| `test_ingestion.py` | 4 | 未归位降级入库 + 飞书告警、无在招职位仍入库 |
| `test_feishu_group_push.py` | 4 | 群推送 payload（有/无 chat_id、@人、文件降级） |

### 2.1 Mock 与真实的分界

| 依赖 | 测试中 | 证据 |
|---|---|---|
| 飞书 | **mock**（`feishu_mode="mock"`，返回合成 id） | `tests/conftest.py:192` |
| 邮件 | **mock**（写 `tmp_path/var/outbox/*.eml`，读 `mock_inbox`） | `tests/conftest.py:193` |
| LLM | **off**（`llm_mode="off"`），需要时用假对象/monkeypatch | `tests/conftest.py:194`、`tests/test_llm_providers.py:78` |
| 数据库 | **真实 PostgreSQL**（与生产同引擎） | `tests/conftest.py:180-178` |
| 网络 | **零出网**（全仓库测试无真实 HTTP；`httpx.AsyncClient` 仅用于 ASGI 内存传输） | `tests/conftest.py:34,232-235` |

因此：**所有"发送成功"的断言只证明代码路径与落库内容，不证明外部服务真打通。**

---

## 3. 未覆盖 / 覆盖不足（按重要性）

| # | 缺口 | 证据 | 影响 |
|---|---|---|---|
| G-1 | **`service/agent.py`（460 行）零直接测试**：没有任何测试文件引用 Agent | `grep -rln "agent" tests/*.py` 无结果 | 自然语言问答/意图路由（2026-11-13 新增的最大模块）只靠手工联调。`FeedbackService` 在 `llm_mode=off` 时 `plan` 恒为 None，测试根本走不到 Agent 分支 |
| G-2 | 真实 SMTP/IMAP 无自动化测试 | 测试全 mock | 163 邮箱的 `ID` 命令、SSL 端口、编码、附件上限都只在人工联调中验证过 |
| G-3 | 真实飞书 API 无自动化测试 | 群推送只断言 payload | 权限错误（如 `99991672` im:resource:upload）只有 live 才暴露 |
| G-4 | 数据库迁移无测试 | 无 `alembic upgrade` 的测试；`hr_robot` 库无 `alembic_version` | 迁移文件是否真能跑通**未经验证**（我只做了列定义比对） |
| G-5 | 并发/多副本无测试 | 无 `max_instances` 之外的并发用例 | 双进程时会重复发简历包（`dispatch_cycle` 无幂等键） |
| G-6 | 大附件/超大简历/多附件邮件无边界测试 | `client/mailbox.py:367-386` 无大小上限 | 单封 20MB 简历可能拖慢整轮（解析有 60s 超时，但 IMAP fetch 没有） |
| G-7 | IMAP 断连重连、水位回退无测试 | `_live_poll` 只 `try/except` 转成 `MailboxUnavailable` | 网络抖动时该轮全部收信失败，下一轮继续（可接受，但无回归保护） |
| G-8 | 飞书回调验签无测试（因为没实现） | `client/feishu.py:412-415` | 见 `05` R-1 |
| G-9 | `messages.status` 的新增取值（`pending_approval`/`superseded`/`cancelled`）无独立状态机测试 | 仅 `test_interview_confirm_gate.py` 5 例间接覆盖 | 状态散落成字符串，改动易漏 |

---

## 4. TODO / FIXME / 未实现

- `TODO` / `FIXME` / `XXX` / `HACK` / `NotImplemented`：**全仓库 0 处**（`grep` 已排除 `.venv` 与 `var/`）。
- `pass` 作为异常兜底 2 处：`client/feishu.py:609`（时间戳解析失败回退当前时间）、`client/mailbox.py:403`（Date 头解析失败忽略）——均属合理兜底，非空实现。
- 明确"不做"的能力清单在 `AGENT.md:11-23` 与 `docs/DECISIONS.md:85-105`，与代码一致（未发现偷偷实现）。

---

## 5. 代码级风险清单（按严重度 × 可复现性）

| # | 风险 | 证据 | 后果 / 触发条件 |
|---|---|---|---|
| K-1 | **分发/通知状态推进不看发送结果** | `service/notification.py:175-192`（dispatch 无论 `feishu_text.ok` 都置 `dispatched`）、`:422-426`（通知失败也置 `interview_notified`） | 飞书/邮件挂掉时，主管看不到简历包但系统认为"已发主管"，下一轮不会再发；需要人工从 `messages.status=failed` 排查 |
| K-2 | 去重只靠 `email`/`phone`，**且两列无唯一索引** | `repository/candidate.py:17-32`、`migrations/versions/0001_initial.py:100-102` | 并发投递（同一人两封邮件同轮处理）可能产生两条 candidate；`find_duplicate` 后无锁 |
| K-3 | `is_duplicate` / `duplicate_of_id` 永不写入 | 全文只有定义 | 任何"按 is_duplicate 统计重复率"的查询都会得到 0 |
| K-4 | 飞书内容级幂等用「收件人+正文」全等匹配 | `repository/message.py:53-71` | 正文里含时间戳/编号时永远匹配不上 → 失败补发可能重复发 |
| K-5 | 线程匹配 `message_id_index LIKE %id%` | `repository/email_thread.py:67-77` | 部分 Message-ID 互为子串时可能误配（概率低，但无边界保护） |
| K-6 | `create_all` 与 Alembic 并存 | `factory/lifespan.py:90`、`migrations/versions/0001_initial.py` | 加字段后老库缺列，启动即 `UndefinedColumn` |
| K-7 | 简历文本截断 50,000 字符 | `service/ingestion.py:264` | 超长简历的语义分/深度解析只看前 50k，可能漏掉关键项目 |
| K-8 | 面试通知确认闸门默认开启 | `config/settings.py:69` | 生产忘关 → 所有面试通知都要人工在飞书回"确认"，HR 漏回则候选人收不到邮件（`pending_approval` 会一直挂着，无超时提醒） |
| K-9 | 改期确认邮件不自动补发 | `service/notification.py:749-755` | 发送失败只能人工处理（这是**有意设计**，防越权；但无提醒机制） |
| K-10 | `notification.positions = None` 死字段 | `service/notification.py:69` | 无害，但会误导维护者以为有注入点 |
| K-11 | Docker CMD 指向不存在的 `main.py` | `Dockerfile:27` | 容器起不来 |
| K-12 | 飞书回调无签名校验 | `client/feishu.py:412-415` + token 为空 | 可被伪造事件触发外发（当前被"人工确认闸门"部分抵消） |

---

## 6. 若要"真正验收"，最小验证动作（建议顺序）

1. 修 venv（Python 3.13）→ 复跑 `pytest -q`，确认仍是 190 passed。
2. `docker compose up -d postgres && python -m alembic upgrade head && python -m alembic check` —— 验证迁移能否在**空库**跑通、模型是否与迁移一致。
3. 修 `Dockerfile` 的 CMD 为 `python -m uvicorn core.main:app --host 0.0.0.0 --port 8000`，`docker compose up --build` 实跑一次 `/health`。
4. 用 `scripts/demo.py --verbose` 在 **mock** 下跑一轮，人工核对 `var/outbox/mail/*.eml` 与 `messages` 表内容。
5. 切 live 前先补 `FEISHU_VERIFICATION_TOKEN` 与签名校验；再用 `scripts/check_mail_login.py` 验证 IMAP/SMTP。
6. live 联调顺序：`/email/test` → `/recruitment/ingest/text` → 群推送 → 主管回复 → `面试通知待确认` → 确认发送。
