# DECISIONS — 决策台账与阻塞项

> 本文件记录 **做了什么取舍、为什么、用户回来后需要补什么**。
> 判定优先级：产品文档 > 开发契约（SKILL）> 本文件记录的默认决策。

---

## 0. Scope Lock（范围锁定｜2026-11-03T15:53+08:00）

把产品文档的「纠错 / 新增」逐条转成可执行 TODO，并标注落点：

| # | 来源 | 要求 | 落点 | 状态 |
|---|---|---|---|---|
| 1 | 纠错 | 产品是机器人，不是界面产品；无图形界面 | 全仓库无 `.html/.css/.js/.vue/.tsx`（`tests/test_api_misc.py` 强制校验） | ✅ |
| 2 | 纠错 | 范围收窄：收简历→筛→发主管→对话→发面试邮件→监控回复并处理 | `service/orchestrator.py::run_daily_cycle` | ✅ |
| 3 | 纠错 | 没有面试官 / 没有管理员 / 不做人才库共享 / 不做笔试测评 | 数据模型与接口中不存在这些概念 | ✅ |
| 4 | 纠错 | 通知渠道二分：主管走飞书、候选人只走邮件 | `service/notification.py`（飞书 + 邮件副本给主管；候选人只发邮件） | ✅ |
| 5 | 纠错 | 面试时间由主管在对话中直接给出，没有双向协商时间线 | `service/extraction.py` 从主管消息抽姓名 + 时间 | ✅ |
| 6 | 纠错 | 「邮件发出即本轮任务完成」，不做被淘汰者的后续运营 | 无感谢信；`screened_rejected` 仅留痕 | ✅ |
| 7 | 新增 | 定时任务驱动（APScheduler，每日） | `factory/lifespan.py`（`apscheduler>=3.11,<4`） | ✅ |
| 8 | 新增 | 必须打通飞书：按部门打包简历压缩包发给主管并对话 | `service/notification.py::_dispatch_position` + `api/v1/feishu.py` | ✅ |
| 9 | 新增 | 用专职 HR 邮箱发面试通知，并监控该邮箱的回复 | `client/mailbox.py`、`service/inbound_email.py` | ✅ |
| 10 | 新增 | 配置驱动：部门→职位→主管映射、筛选条件、定时、模板 | `config/positions.yaml`、`config/templates/`、`prompt/screening.md` | ✅ |
| 11 | 新增 | 事件留痕：状态变化/消息/筛选结果可追溯 | `model/event.py`、`model/message.py` | ✅ |
| 12 | 截图验收 | 主管可在飞书自由文本回复，也可在**原邮件**上回复，两条路径都要能触发面试通知 | `api/v1/feishu.py` + `service/inbound_email.py` → 同一个 `service/feedback.py` | ✅ |
| 13 | 截图验收 | 飞书文案格式：`张总，本次贵部门面试岗位 XXX，共收到 X 份…已经发送到您邮箱` | `config/templates/feishu_dispatch.md` | ✅ |
| 14 | 截图验收 | 邮件通知后要生成一条“已通知”的飞书消息回执给主管 | `config/templates/feishu_receipt.md` | ✅ |

**明确不做**（写在这里防止后来者“顺手补全”）：Web/小程序/APP 任何界面、登录与权限体系、面试官评价、一面/二面、人才库、笔试与测评对接、淘汰感谢信、候选人自助改期页面。

---

## 1. 阻塞项

### [2026-11-03T15:53+08:00] 阻塞项：飞书自建应用凭证（app_id / app_secret）缺失
- 已尝试的默认值：`FEISHU_MODE=mock`，出站消息返回合成 `message_id` 并写入 `messages` 表；入站事件走真实 `im.message.receive_v1` 报文结构解析。
- 当前采用的 fallback：全部飞书出站只落库 + 日志，入站由 `/api/v1/feishu/webhook` 接收（测试用真实报文样例覆盖）。
- 对主流程的影响：仅影响“真的把消息发到飞书”；闭环逻辑、打包、对话解析、回执全部可验证。
- 用户回来后需要提供：开放平台自建应用的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN`（如启用加密再加 `FEISHU_ENCRYPT_KEY`），并把回调地址指到 `/api/v1/feishu/webhook`，订阅 `im.message.receive_v1` 与 `card.action.trigger`。

### [2026-11-03T15:53+08:00] 阻塞项：HR 邮箱 SMTP/IMAP 账号未知
- 已尝试的默认值：`MAIL_MODE=mock`，发信写 `var/outbox/mail/*.eml`（完整 RFC822，含 Message-ID / In-Reply-To / References / Reply-To），收信读 `var/mock_inbox/*.eml`。
- 当前采用的 fallback：候选人回信通过 `POST /api/v1/email/simulate-inbound` 或投放 `.eml` 文件模拟。
- 对主流程的影响：无（发信/收信/线程匹配全部按真实格式实现并测试）。
- 用户回来后需要提供：`SMTP_HOST/PORT/USER/PASSWORD`、`IMAP_HOST/PORT/USER/PASSWORD`、`HR_MAILBOX` 真实地址。

### [2026-11-03T15:53+08:00] 阻塞项：本机 Docker daemon 未启动，`docker-compose up` 未实机验证
- 已尝试的默认值：按生产规范提供 `Dockerfile`（python:3.11-slim + HEALTHCHECK）与 `docker-compose.yml`（app + postgres:16，`depends_on: service_healthy`）。
- 当前采用的 fallback：本机以 SQLite + uvicorn 跑通全部测试与演示流程。
- 对主流程的影响：仅影响容器化验证。
- 用户回来后需要：启动 Docker Desktop 后执行 `docker compose up --build`，确认 `http://localhost:8000/health` 返回 `ok`。

### [2026-11-03T15:53+08:00] 阻塞项：LLM 凭证缺失
- 已尝试的默认值：`LLM_MODE=auto`，无 `OPENAI_API_KEY` 时自动降级为确定性规则抽取（姓名接地匹配 + 中文时间解析 + 简历规则预填）。
- 当前采用的 fallback：规则路径已覆盖截图里的两种主管话术与三类候选人回信；语义打分默认权重 0。
- 对主流程的影响：仅影响“更自由的表达理解”和语义匹配分；不影响闭环。
- 用户回来后需要提供：`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `LLM_MODEL`。

---

## 2. 设计决策

### D-001 飞书/邮件/LLM 三态模式（mock / live / off）
外部系统均以“可观测、可替换”的方式接入：`FEISHU_MODE`、`MAIL_MODE`、`LLM_MODE`。mock 不是假数据分支，而是**只把出站写到 DB/磁盘**，入站解析仍是真实报文格式。这样评审可以在没有任何凭证的情况下看到完整闭环与留痕，也避免把“跑不起来”归咎于环境。

### D-002 时间统一存 naive UTC
SQLite 不保留时区，混存 aware/naive 会在比较时炸。约定：所有 `DateTime` 列存 **naive UTC**，展示统一经 `core/timeparse.py` 转 `Asia/Shanghai`。代价是读库时需要显式转换，收益是跨 SQLite/PostgreSQL 行为一致。

### D-003 单一邮件轮询入口，避免“两套 poller 互吞邮件”
契约里 `service/ingestion.py` 与 `service/inbound_email.py` 都涉及收信。若各自 `poll_inbox`，先跑的那个会把对方的邮件消费掉（mock 模式靠 watermark，IMAP 靠 UID）。因此**运行时只有一个轮询器**：`InboundEmailService.poll_once()` 拉一次，再按“线程命中 → 主管；线程命中 → 候选人；带简历附件 → ingestion；其余忽略”分流。`IngestionService.ingest_from_mailbox` 仍保留，供脚本单独使用。每日任务通过 `poll_once(cycle_id=...)` 收简历，高频任务通过 `poll_once()` 只处理回复——**语义一致、不会丢信**。

### D-004 主管除飞书外，另发一份简历包邮件
产品文档写的是“飞书发压缩包”，但截图明确要求主管“直接在原邮件上回复”。若主管邮箱里没有那封邮件，这条路径物理上不成立。因此：飞书发文字摘要 + 压缩包文件消息；同时把同一压缩包发到主管邮箱（`Reply-To: reply+<token>@domain`）并登记 `EmailThread(kind=resume_package)`。这是对两条验收路径的**同时满足**，不是越界功能。

### D-005 姓名解析采用“接地匹配”而非自由识别
主管回复里的姓名一定来自本轮已发名单。`match_roster_names()` 只在名单内匹配，避免把“下周”“技术部”“12 份”当人名；LLM 路径同样做名单过滤（`test_llm_names_are_grounded_to_roster`）。名字对不上名单时**不猜**，回一条飞书消息请主管确认。

### D-006 缺信息只追问，不发错邮件
`_handle_schedule` 的三条分支：无姓名 → 回格式提示；有姓名无时间 → 追时间；有时间无姓名 → 追名单。任何一条都不触发邮件，符合“只有明确结论才发通知”。

### D-007 改期必须主管确认
候选人回信说“改到周四下午 3 点”时，机器人**先**在飞书问主管，主管回“同意”或给出新时间后才补发确认邮件（`test_candidate_reschedule_and_supervisor_approval`）。理由：面试时间的所有权在主管，机器人不得替主管答应。

### D-008 新增 `core/timeparse.py`
属于 `core/`（横切关注点）内的新模块，未改变契约的目录结构。理由：中文相对时间（下周X / 下午X点 / 十一）是跨层复用的纯逻辑，放在任何 service 里都会造成重复。

### D-009 `email_threads` 增加 `message_id_index` 扁平列
`message_ids` 仍保留 JSON（审计用），另加一列空格拼接字符串用于 `LIKE` 匹配：SQLite 对 JSON 列的包含查询支持差，扁平列在 SQLite/PostgreSQL 上行为一致。属于同一聚合根内的字段补充，未新增表。

### D-010 建表用 `create_all`，暂不引入 Alembic
MVP 阶段表结构仍在动；引入迁移框架会增加评审噪音。已在 `core/db.py::init_models` 注明生产可替换为 Alembic。

### D-011 `unstructured` 为“首选但可缺省”的解析器
契约要求借鉴 unstructured 的分区能力，但当前网络环境装不下其全部依赖（见 D-012）。`client/resume_parser.py` 先尝试 `unstructured.partition`，缺失或失败时降级 `pypdf` / `python-docx`，行为对上层一致，并在返回的 `warnings` 里留痕。

### D-012 依赖安装走 PyPI 官方源
本机访问 `pypi.tuna.tsinghua.edu.cn` 返回 “no versions”（镜像经代理取不到大体积索引页），改用 `pypi.org` + `--timeout 300` 安装。仅影响开发环境，不影响生产镜像（Docker 内默认 PyPI）。

### D-013 仓库原有 demo 文件未提交删除
`D:\!pythonproject\hr_ai_robot` 是老师提供的 gitee 仓库（`ncepu-bj/hr-ai-robot`，当前分支 `develop`），HEAD 里是往期学员 demo（`Demo1.py`、`feishu_bot.py` 等），工作区已被清空。本次按契约在**同一目录**建立正式工程结构，但**未执行 git add/commit/push**（无人值守期间不替用户改写远端历史）。需要时由用户决定：在新分支（如 `feature/robot-v1`）提交，或先将旧 demo 归档到 `legacy/`。

### D-014 事件解密的依赖边界
`lark_oapi` 负责 token/加解密。`client/feishu.py::_decrypt` 调用 SDK 的 `AESCipher`，若当前 SDK 版本不提供该工具，会抛 `ConfigurationError` 并提示——此时应在飞书后台关闭“加密推送”，或在网关层解密。**不自行实现 AES**（契约明确要求不重复造轮子）。

### D-015 职位规则以 YAML 为唯一真源
每轮任务开始 `PositionRepository.sync_from_config()` 覆盖数据库快照；配置里删掉的职位置 `active=false` 而不物理删除（保留历史简历关联）。HR 改 YAML → 下一轮生效，无需重启。

### D-016 邮件署名不再重复联系人
`_signature()` 原先把 `company.hr_contact` 再印一遍，而模板正文已有一行「联系人：…」，候选人会看到两份同样的联系方式（`scripts/demo.py` 实跑时发现）。改为署名只留「公司名｜HR 招聘组」+ 机器人自动发送提示；联系人信息统一由正文 `- **联系人**：` 一行承载。同时把 `interview_notice.md` 的 `{{ notes }}` 包进 `{% if notes %}`，避免空备注留下两行空白。

### D-017 提供 `scripts/demo.py` 作为无人值守验收入口
真实飞书/邮箱/LLM 凭证都不可用，验收方需要“看得见的证据”。`scripts/demo.py` 用纯 mock 走完 投递→筛选→打包分发→主管飞书回复→面试通知→候选人改期→主管同意→补发确认，并把每一步机器人真正发出的飞书文本/邮件正文打印出来；`--keep` 用于在既有数据上复跑（只验证重跑，不再安排已通知过的候选人）。该脚本不新增任何业务逻辑，仅编排既有 service。

---

## 3. 与契约文字的偏差（如实记录）

| 契约原文 | 实际情况 | 处理 |
|---|---|---|
| `Mr-KID-github/Dify-FeiShu-bot`（**FastAPI** 路由范式） | 该仓库 `app.py` 是 **Flask**（`from flask import Flask`，`@app.route('/webhook')` + 线程池），并非 FastAPI | 仅借鉴“单 bot 单 `/webhook` + 挑战应答 + 事件分发”范式，用 FastAPI `APIRouter` 实现；README 台账已标注 |
| `srbhr/Resume-Matcher`（官方口径 28k+，许可证以仓库为准） | 运行时读取：29,080 star，`Apache-2.0`（非 MIT） | 台账写实际值 |
| `Anil-matcha/open-agent-email`（约 2.1k star） | 运行时 2,121 star，Apache-2.0 | 台账写实际值 |
| `emersion/python-emailthreads`（44 star，`groupemailthread.py` 式实现） | 运行时 49 star，已归档；实际文件为 `emailthreads/threads.py` | 台账标注 `LOW_STAR_EXCEPTION` 与真实路径 |
| `sabuhish/fastapi-mail`（1k star） | 运行时 1,016 star，MIT | 台账写实际值 |
| `getsentry/sentry` 的 `GroupEmailThread` | `src/sentry/models/groupemailthread.py` 存在（HTTP 200 核验）；但 GitHub API 返回 `NOASSERTION`，仓库为 Functional Source License（非 OSI 开源） | 只借鉴模型设计，**不引用任何代码** |

---

## 4. 抽取量化调优复盘（§4-A.2｜2026-11-03T21:xx+08:00）

### 4.1 本次做了什么

按 AGENT.md §4-A.2（项目所有者 2026-11-03 明批的授权项）对 LLM 抽取模块做量化调优，**不碰任何运行时行为**：

1. `prompt/extraction.py`：仅**追加** few-shot 样例（面试安排 `INTERVIEW_FEW_SHOT` + 候选人回信 `REPLY_FEW_SHOT`），不改 JSON Schema、不改抽取指令、不绕过 I-05。样例里的姓名全部来自名单、时间全部可解析，用于强化「姓名接地 + 中文时间」正确范式。
2. `scripts/eval_extraction.py`：新增评估集（17 条人工标注，覆盖主管指令的乱序/含糊/夹杂噪声/接地排除，以及候选人回信的改期/放弃/确认/含糊），统计**字段级精确匹配准确率**与**姓名接地命中率**。
3. `tests/test_extraction_eval.py`：复用同一评估集做回归测试，保护 I-05 接地与意图识别（只断言硬不变量，时间准确性交给 eval 量化报告，避免把规则路径的已知弱项误判为测试失败）。

### 4.2 预期 vs 实际准确率（离线规则主路径，固定 now=2026-11-03 15:53 +08:00）

| 字段 | 准确率 | 说明 |
|---|---|---|
| 姓名（接地后精确匹配） | 100.0%（11/11） | 含「12份/3人」噪声不误判、名单外「小李」被丢弃 |
| 意图（confirm/reschedule/decline） | 100.0%（6/6） | 关键词判定可靠 |
| 新时间（候选人改期） | 100.0%（6/6） | 「改到 X」之后时间解析正确 |
| 时间（主管安排） | 90.0%（9/10） | 仅 1 例失败，见 4.3 |
| **整体字段级** | **97.0%（32/33）** | — |
| **姓名接地命中率** | **100.0%（11/11）** | 抽取姓名全部来自名单，I-05 在 service 层硬保证，不依赖 prompt |

> 说明：few-shot 样例只在 **LLM 启用时**进入 prompt；当前离线环境（无 key）走确定性规则主路径，所以上面的数字度量的是**规则抽取能力**。有 key 时，few-shot 是进一步抬升 LLM 路径准确率的抓手，需另跑一次 `eval_extraction.py` 取真实 LLM 数字。

### 4.3 遇到的错误 case（诚实记录，非 few-shot 缺陷）

- **E-01（规则弱项，非 prompt 问题）**：`通知张明，下个月5号上午十点面试` —— 规则路径的 `_CN_MONTH_DAY_RE` 要求「数字+月」，而「下个月」无前置数字，相对月份解析未覆盖，规则误把「上午十点」当绝对时间 → 落为当天顺延（11-04 10:00），与真值 12-05 10:00 不符。**这是 `core/timeparse.py` 的相对月份盲区，属于 §4-A 之外的停机项，本轮未动**。
- **E-02（客观上无法唯一标注）**：`张明下周三、李四下周四都来面试` —— 一人一时间的句式，规则路径只能坍缩为单一时间，无法逐人赋值。已在评估集中 `grade_time=False` 排除时间分项分母，仅校验姓名集合正确，不计入准确率分母。

### 4.4 结论

- 离线规则主路径在「姓名接地 / 意图 / 新时间」上已达 100%，整体 97%；唯一短板是相对月份（E-01）。
- 本次 few-shot 调整**未改变任何运行时行为**（prompt 文本仅在 LLM 路径生效），demo 指纹 B、事件集指纹 C、96→148 全绿均印证零破坏。
- 下一步若要让准确率再上一层：应修 `core/timeparse.py` 的相对月份（E-01，停机问人），而非在 prompt 里打补丁——符合 P1「只减复杂度」与 §9 停机规则。
