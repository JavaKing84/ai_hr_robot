# 00 · 项目事实底稿（PROJECT BRIEF）

- 生成时间：2026-11-14T00:53:52+08:00
- 仓库路径（本机）：`D:\!pythonproject\hr_ai_robot`
- 当前分支：`develop`
- 当前 commit：`bf2cc81fb7b18aafc45c2e47ae844def216e4b8e`（2026-11-13 20:58:20 +0800，"飞书自然语言，以及云数据库测试"）
- `git status --short`：**0 条改动**（工作区干净，无未提交/未跟踪内容；本次新增的 `project-intel/` 为本产物目录）
- 扫描范围：仓库内全部 `.py`（**105 个文件 / 16,818 行**，`git ls-files '*.py'` 实测）；其中业务代码 **67 个 / 11,280 行**（`api`6 `client`5 `config`2 `core`9 `factory`5 `middlewares`4 `model`8 `repository`7 `schema`8 `service`10 `prompt`3），另 `tests`19/2,868、`scripts`17/2,374、`migrations`2/296。含 `migrations/`、`prompt/`、`docs/`（17 篇）、`docker-compose.yml`、`Dockerfile`、`pytest.ini`、`alembic.ini`、`.env.example`
- 排除范围：`.venv/`、`.git/`、`.pytest_cache/`、`.idea/`、`var/`（运行时产物，含历史备份与旧 SQLite）、`tests/testdata/`（.gitignore 未跟踪）、`.env` 的**取值**

> 本次所有结论的证据形式在正文标注：`直接证实`（源码/命令输出）、`文档描述`（仅文档写着）、`运行证实`（本次实际执行）、`推断`（源码未写死的设计意图）。

---

## 1. 项目实际用途

**产品目标**（`README.md:5`、`AGENT.md:9`，文档描述）：一个无人值守的招聘机器人。每天定时从 HR 邮箱（含招聘平台转发邮件）收简历 → 结构化解析 → 按职位规则筛选 → 按部门打包 zip 发飞书主管（另发一份主管邮箱副本）→ 主管在飞书或原邮件里回名单和时间 → 机器人发面试通知邮件 → 持续盯 HR 邮箱处理候选人改期/放弃。**没有任何前端界面**（`AGENT.md:11-23` 明确"绝对不做"，`tests/test_api_misc.py` 校验仓库无前端文件）。

**源码已实现能力**（直接证实）：

| 能力 | 实现位置 | 状态 |
|---|---|---|
| IMAP 增量收信（UID 水位）+ mock 收信 | `client/mailbox.py:214-306` | 已实现 |
| 入站邮件四类分流（简历 / 主管反馈 / 候选人回信 / 忽略） | `service/inbound_email.py:109-181` | 已实现 |
| 简历解析（unstructured → pypdf → python-docx 降级，子进程隔离） | `client/resume_parser.py:513-593`、`core/safe_process.py:97-186` | 已实现 |
| 结构化抽取（规则先跑 + LLM 补全，字段级合并策略） | `service/extraction.py:121-211` | 已实现 |
| 职位归位（指定职位 > 主题/附件名岗位名 > 简历意向 > 关键词投票） | `service/ingestion.py:486-546` | 已实现 |
| 去重（邮箱或手机号命中即视为重复，只追加事件） | `service/ingestion.py:220-245`、`repository/candidate.py:17-32` | 已实现（**但未写入 `is_duplicate`/`duplicate_of_id` 列**，见 §5 差异 D-3） |
| 规则筛选（硬性条件 + 加权归一化打分 + 阈值） | `service/screening.py:136-275` | 已实现 |
| 语义分 + 深度解析（影子模式，不推翻硬条件） | `service/screening.py:358-506` | 已实现 |
| 按职位打包 zip（含 `00-筛选说明.md`）+ 飞书群推送 + 主管邮件副本 | `service/notification.py:87-333` | 已实现 |
| 主管反馈解析（飞书自由文本 / 飞书卡片 / 邮件原回复三入口同一用例） | `service/feedback.py:112-208` | 已实现 |
| 面试通知「人工确认闸门」（默认开启，未确认不投递） | `service/notification.py:387-397,464-583` | 已实现 |
| 候选人回信改期 / 放弃 / 确认 | `service/inbound_email.py:184-260`、`service/feedback.py:610-684` | 已实现 |
| 失败消息幂等补发（邮件按原 Message-ID，飞书按收件人+正文查重） | `service/notification.py:729-821` | 已实现 |
| 飞书自然语言问答助手（路由 → 检索 → 作答，模型不碰库） | `service/agent.py:93-457` | 已实现（LLM 不可用时静默退场） |
| 三张留痕表 + HTTP 运维查询接口 | `model/*.py`、`api/v1/recruitment.py` | 已实现 |

**仅文档描述 / 未发现实现**（直接证实为"未找到"）：
- 面试官角色、一面二面、面试评价、笔试测评、人才库、候选人自助改期页、淘汰感谢信：`AGENT.md:11-23` 明确不做，代码中亦无对应模块。
- 招聘平台 API 对接（Boss/51job 开放接口）：无。平台简历只通过"转发邮件/通知邮件"进入 IMAP。
- 多副本部署的分布式调度：无（`factory/lifespan.py:48-82` 为进程内 `AsyncIOScheduler` 内存 job store）。

---

## 2. 技术栈与版本

**声明版本**（`requirements.txt`，直接证实）：
`fastapi>=0.110`、`uvicorn[standard]>=0.27`、`sqlalchemy[asyncio]>=2.0`、`asyncpg>=0.29`、`aiosqlite>=0.19`、`alembic>=1.13`、`pydantic>=2.6`、`pydantic-settings>=2.2`、`email-validator>=2.1`、`apscheduler>=3.11,<4`、`aiosmtplib>=3.0`、`jinja2>=3.1`、`pyyaml>=6.0`、`httpx>=0.27`、`python-multipart>=0.0.9`、`pypdf>=4.0`、`python-docx>=1.1`、`lark-oapi>=1.7.5`、`litellm>=1.50`、`unstructured[pdf,docx]>=0.14`。
测试依赖 `requirements-dev.txt`：`pytest>=8.0`、`pytest-asyncio>=0.23`（`-r requirements.txt`）。

**实际运行环境**（运行证实）：
- 解释器：`D:\!pythonproject\hr_ai_robot\.venv\Scripts\python.exe` = **Python 3.13.14**
- 镜像声明：`Dockerfile:1` `python:3.11-slim`（与本地 venv 3.13 不同；`AGENT.md:30` 要求 >=3.11）
- 数据库：PostgreSQL 16（`docker-compose.yml:5` `postgres:16-alpine`）；本机 5432 端口上运行的是名为 `pgsql` 的 `postgres:16-alpine` 容器（非本仓库 compose 起的 `postgres` 服务），`hr/hr` 账号可连（运行证实）
- LLM：`litellm` 多供应商链；缺 litellm 时回落 `httpx` 直连 OpenAI 兼容 `/chat/completions`（`client/llm.py:169-261`）
- 飞书：`lark_oapi` 官方 SDK（`client/feishu.py:46-65`）

---

## 3. 启动入口与运行方式

| 方式 | 命令 | 证据 |
|---|---|---|
| 开发/本地（README 推荐） | `python -m uvicorn core.main:app --host 127.0.0.1 --port 8000` | `README.md:380`；`core/main.py:10` `app = create_app()` |
| 直接跑入口模块 | `python -m core.main` | `core/main.py:13-19` 调 `uvicorn.run` |
| 容器 | `docker compose up --build` | `docker-compose.yml`；**注意 Dockerfile 的 CMD 指向不存在的 `main.py`**，见 §5 差异 D-1 |
| 手动跑一轮闭环（不起服务） | `python scripts/run_cycle_once.py` | `scripts/run_cycle_once.py:1` |
| 全 mock 演示 | `python scripts/demo.py --verbose` | `README.md:377` |
| 建表 | `python -m alembic upgrade head` / `python scripts/init_db.py` | `README.md:373`、`scripts/init_db.py:21` |
| 测试 | `python -m pytest -q`（需 PostgreSQL 在线） | `pytest.ini`、`tests/conftest.py:60-77` |

调度：`factory/lifespan.py:85-108` 启动时先 `init_models()`（= `create_all`），再按 `SCHEDULER_ENABLED` 启 `AsyncIOScheduler`；注册两个 job——`hr_daily_cycle`（cron，默认 `0 9 * * *`）与 `hr_inbound_poll`（interval，`INBOUND_POLL_SECONDS`，<=0 不注册）。

---

## 4. 主要业务流程（一句话版，详见 `03_BUSINESS_FLOWS.md`）

`Orchestrator.run_daily_cycle()`（`service/orchestrator.py:73-161`）：
1. 记 `cycle.started` 事件 → `sync_config()`（重读 `config/positions.yaml` 并 upsert 进库，`repository/position.py:35-108`）
2. `inbound.poll_once(cycle_id)`：一次轮询处理**所有**入站邮件（简历入库 / 主管反馈 / 候选人回信），避免两套 poller 互吞邮件
3. `notification.dispatch_cycle(cycle_id)`：按职位分组打 zip → 飞书文本 + 飞书文件 + 主管邮件副本 → 候选人置 `dispatched`
4. `retry_failed_interview_notices()` + `retry_failed_feishu_messages()`：幂等补发上一轮失败
5. 汇总 `RunCycleResponse` 并记 `cycle.completed`

主管反馈（飞书 webhook / 邮件线程命中）→ `FeedbackService.handle()`：
确认/取消面试通知 → 自然语言路由（读类由 Agent 作答、写类走确定性代码）→ 淘汰 → 改期决策 → 安排面试（缺姓名追问、缺时间追问，齐全才发通知）。

---

## 5. 与文档不一致之处（本次核对出的硬差异）

| # | 文档说法 | 源码/仓库事实 | 证据 |
|---|---|---|---|
| D-1 | `Dockerfile:27` `CMD ["python", "main.py"]`；`core/main.py:1` 注释"原根目录 main.py" | 仓库根目录**没有** `main.py`（`ls *.py` 为空），容器可能起不来 | `Dockerfile:27`、`core/main.py:1`、根目录清单 |
| D-2 | `AGENT.md:188`、`docs/design/architecture.md:4,177,209`、`docs/DECISIONS.md:13,40`、`service/ingestion.py:376` 均称状态枚举 **10 个冻结** | `model/candidate.py:20-61` 实际 **11 个**（多 `interview_pending_confirm`，`CandidateStatus.ALL` 有 11 项），且它在 flow 中被真实使用（`service/notification.py:482`、`service/feedback.py:686`） | 见左 |
| D-3 | `model/candidate.py:4-5` 称重复投递时用 `is_duplicate`/`duplicate_of_id` 标出关系 | 代码从不给这两列赋值：`duplicate_of_id` 只出现在 model 与迁移里，重复投递**根本不新建行**（`service/ingestion.py:240-245` 直接返回首次记录 + 一条事件），只有 `IngestOutcome.is_duplicate` 这个内存字段 | `grep -rn "duplicate_of_id"` 仅 3 处定义 |
| D-4 | `AGENT.md:205`、`docs/DECISIONS.md:40` 称"建表用 create_all，未引入 Alembic" | 仓库已有 `alembic.ini` + `migrations/versions/0001_initial.py`；但**运行时启动仍然调 `init_models()`（create_all）**（`factory/lifespan.py:90`），即两套建表路径并存 | 见左 |
| D-5 | `README.md:7` 指向根目录 `API.md`、`DECISIONS.md` | 实际在 `docs/API.md`、`docs/DECISIONS.md`；根目录只有 `README.md`、`AGENT.md` | `ls *.md` |
| D-6 | `docs/quality/testing-report.md:5,94` 称 **154** 个用例；`README.md:441` 称 **190** 个 | 实测收集 **190** 个（`pytest --collect-only`），全绿 | 运行证实 |
| D-7 | `docs/quality/testing-report.md:73` 写项目根目录为 `E:\project\hr-ai-robot` | 本机实际在 `D:\!pythonproject\hr_ai_robot` | 运行证实 |

> D-1/D-2/D-3 属"文档落后于代码"或"注释夸大实现"，**以源码为准**，本报告不修改代码。

---

## 6. 当前工作区状态与运行态配置

- 工作区干净（0 改动），当前分支 `develop`，最近一次提交即本次扫描的 HEAD。
- `.env` 存在且**已从 mock 切到 live**（运行证实，只记录布尔态不记录取值）：
  - `FEISHU_MODE=live`、`FEISHU_PUSH_TARGET=group`、`FEISHU_CHAT_ID` 与 `FEISHU_WEBHOOK_URL` 均已配置
  - `MAIL_MODE=live`，`SMTP_*` / `IMAP_*` 均已配置
  - `LLM_MODE=live`，`LLM_PROVIDERS` 解析出 **3** 家供应商（`llm_provider_chain` 长度 3）
  - `INTERVIEW_NOTICE_REQUIRE_CONFIRM` 生效为 `true`；`SEMANTIC_SHADOW` 生效为 `true`；`LLM_DEEP_ANALYSIS=true`
  - `INBOUND_POLL_SECONDS=0`（→ 入站定时轮询**未注册**，只有每日 cron + 手动/HTTP 触发收信）
- 业务配置 `config/positions.yaml`：3 部门 / 5 职位（`backend_senior`、`algorithm_engineer`、`python_developer`、`product_manager`、`marketing_specialist`），阈值 60/60/55/55/45，每个职位均配置 `semantic_weight`（30/35/30/25/25），`backend_senior` 配了 1 个 `always_pass_names` 白名单。
- 数据库：`.env` 的 `DATABASE_URL` 指向 `localhost:5432/hr_robot`（`hr/hr`），测试库 `hr_robot_test` 已存在且可连。

> 因 `.env` 处于 live 态，**本次未执行任何"发信/发飞书/调模型"的动作**：测试全程走 conftest 强制的 `feishu_mode=mock`、`mail_mode=mock`、`llm_mode=off`（`tests/conftest.py:184-200`）。

---

## 7. 关键限制与未确认项（详见 `09_OPEN_QUESTIONS.md`）

1. 分发包状态推进不看发送结果：`service/notification.py:175-192` 无论飞书/邮件是否 `ok`，都把候选人置 `dispatched`（仅把错误汇总进返回值）。
2. 单进程调度，无分布式锁；多副本会重复触发（`AGENT.md:204` 自述）。
3. 飞书回调只校验 verification token，**未见签名校验**（`client/feishu.py:402-416`）；加密体依赖 SDK 的 `AESCipher`，缺失即 `configuration_error`。
4. 沙箱外真实联调证据缺失：飞书群推送 / SMTP 实发 / 真 IMAP 轮询在本次扫描中**未运行**，仅静态核对。
5. `.env` 处于 live 且含真实凭据，任何"重跑演示脚本"的动作都可能真实外发。
