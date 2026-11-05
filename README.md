# ai-hr｜飞书 HR 招聘机器人

> 服务器上按定时任务运行的 AI 自动程序。**没有图形界面**——它的界面是飞书对话与邮件。
> 每天自动：收简历 → 解析筛选 → 按部门打包发主管（飞书）→ 主管回话给出名单与时间
> → 自动给候选人发面试通知邮件 → 持续监控 HR 邮箱，处理改期/放弃并回报主管。
> **邮件发出即本轮任务完成**，其余一概不做。

本项目严格按产品文档《ai-hr｜飞书 HR 招聘机器人产品制作与智能体交接文档》与开发契约实现，
开发过程中的取舍、偏差与阻塞项全部记录在 [`DECISIONS.md`](./DECISIONS.md)。

---

## 1. 边界（明确不做）

| 不做 | 原因 |
|---|---|
| Web / 小程序 / APP 任何界面 | 产品形态是机器人，界面即对话；仓库内由测试强制校验无 `.html/.css/.js/.vue/.tsx` |
| 面试官角色、一面/二面、面试评价 | 产品文档明确“没有面试官” |
| 管理员、人才库共享 | 明确“不需要管理员” |
| 笔试 / 测评系统对接 | 不在闭环内 |
| 被淘汰候选人的感谢信 | 默认不做，仅保留 `screened_rejected` 状态与事件 |
| 候选人自助改期页面 | 候选人只收发邮件，没有入口 |

角色与渠道：**主管走飞书**（内部通讯）；**候选人只走邮件**（没有飞书）；HR 邮箱是机器人自己的收发通道。

---

## 2. 闭环

```
┌──────────────┐   ┌────────────┐   ┌──────────────┐   ┌────────────────────┐
│ 09:00 定时触发 │ → │ IMAP 收简历 │ → │ 解析+归位+筛选 │ → │ 按职位打 zip + 摘要  │
└──────────────┘   └────────────┘   └──────────────┘   └─────────┬──────────┘
                                                                  │ 飞书消息 + 压缩包
                                                                  │ 邮件副本（同一压缩包）
                                                                  ▼
                                          ┌───────────────────────────────────┐
                                          │ 主管回复（两条路径，同一套解析）      │
                                          │ A. 飞书直接回复自由文本              │
                                          │ B. 在原简历邮件上回复同样内容         │
                                          └───────────────┬───────────────────┘
                                                          │ 姓名（接地到本轮名单）+ 面试时间
                                                          ▼
                                          ┌───────────────────────────────────┐
                                          │ 候选人逐个收到面试通知邮件（模板化）   │
                                          │ 同时回一条飞书“已通知”回执给主管      │
                                          └───────────────┬───────────────────┘
                                                          │ 候选人回信
                                                          ▼
                                          ┌───────────────────────────────────┐
                                          │ 意向解析：改期 / 放弃 / 确认         │
                                          │ 改期 → 先问主管 → 确认后补发确认邮件  │
                                          │ 放弃 → 飞书通知主管                 │
                                          └───────────────────────────────────┘
```

关键取舍：**时间只能来自主管**。机器人不猜时间、不替主管答应改期；缺姓名或缺时间时只回消息追问，
绝不发错邮件（见 `DECISIONS.md` D-005/D-006/D-007）。

---

## 3. 快速开始

### 3.1 本地（无任何外部凭证即可跑通全闭环）

```bash
cd D:\!pythonproject\hr_ai_robot
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt

copy .env.example .env          # 默认 FEISHU_MODE=mock / MAIL_MODE=mock / LLM_MODE=auto

.venv\Scripts\python scripts\seed.py --reset      # 建表 + 测试公司（3 部门 / 3 主管 / 4 职位）
.venv\Scripts\python scripts\ingest_sample.py     # 投 6 封模拟简历邮件到 var/mock_inbox
.venv\Scripts\python main.py                      # http://127.0.0.1:8000/health

curl -X POST http://127.0.0.1:8000/api/v1/recruitment/run     # 手动触发一轮闭环
```

`mock` 模式的语义：**飞书/邮件不发真实请求，但出站内容完整落库/落盘**，
入站仍是真实报文格式（`.eml`、飞书事件 JSON），因此闭环逻辑与真实环境一致。

想一眼看完整个闭环（投递 → 筛选 → 打包发主管 → 飞书回复名单 → 发面试通知 → 候选人改期 → 主管同意 → 补发确认），
不必起服务、不必调接口：

```bash
.venv\Scripts\python scripts\demo.py           # 重建库后逐步骤打印机器人真正发出去的每一条内容
.venv\Scripts\python scripts\demo.py --verbose # 同时打印结构化运行日志
.venv\Scripts\python scripts\demo.py --keep     # 保留现有数据，在当前库上再跑一轮
```

输出形如（节选）：

```
  · 批次 cycle_id=202611031631｜收到 6 份｜通过 5｜淘汰 1｜重复 0
  · ✓ 张伟 / 高级后端工程师：收到 3 份、通过 2 份（张明、李四）→ 飞书已发｜主管邮件已发
  · 机器人判定动作：interview_scheduled｜已处理=True
  · interview_time = 2026年11月11日（周三）14:00
```

查看机器人到底发了什么：

```bash
curl "http://127.0.0.1:8000/api/v1/recruitment/messages?channel=feishu&direction=outbound"
curl "http://127.0.0.1:8000/api/v1/recruitment/messages?channel=email&direction=outbound"
# 真实 RFC822 副本在 var/outbox/mail/*.eml，简历包在 var/packages/<cycle_id>/*.zip
```

### 3.2 Docker

```bash
copy .env.example .env
docker compose up --build
curl http://localhost:8000/health
```

`app` 与 `postgres:16` 一起起，`depends_on: service_healthy`，容器自带 `HEALTHCHECK`。

### 3.3 主要环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://hr:hr@localhost:5432/hr_robot`（本地/生产）；测试用 SQLite（由 `tests/conftest.py` 注入） | PostgreSQL |
| `FEISHU_MODE` | `mock` / `live` | `mock` |
| `MAIL_MODE` | `mock` / `live` / `off` | `mock` |
| `LLM_MODE` | `auto`（有 key 才调）/ `live` / `off` | `auto` |
| `SCHEDULE_CRON` | 每日触发时间（5 段 cron） | `0 9 * * *` |
| `INBOUND_POLL_SECONDS` | 入站邮件轮询间隔，`0` 关闭 | `300` |

---

## 4. 目录结构与职责

```
main.py                 仅 create_app() + uvicorn
factory/                应用工厂：app / lifespan(APScheduler) / routers / middleware
middlewares/            request_id、access_log、exception（统一错误信封）
api/v1/                 仅 HTTP 解析与依赖注入：recruitment / feishu / email
schema/                 Pydantic v2：请求响应、抽取 schema、配置 schema、筛选结果
service/                用例编排：ingestion / screening / notification / email_threading
                        / inbound_email / extraction / feedback / orchestrator
model/                  SQLAlchemy ORM：candidate / position / supervisor / message / event / email_thread
repository/             单聚合根数据访问（禁止调用外部 API）
client/                 feishu / mailbox / llm / resume_parser
prompt/                 screening.md（HR 可改的筛选模板）+ extraction.py（抽取提示）
core/                   db / deps / exceptions / logging / threading / timeparse
config/                 settings.py + positions.yaml + templates/*.md
scripts/                seed / init_db / ingest_sample / demo（一键跑完整个闭环）
tests/                  81 个用例（pytest + pytest-asyncio + httpx）
```

分层规则：`api` 不写业务；`service` 不直接拼 SQL；`repository` 不调外部 API；
`client` 不碰数据库。所有外部系统都有 mock 实现，因此测试完全离线。

---

## 5. 配置：HR 只改这两处

### 5.1 `config/positions.yaml`（部门 → 职位 → 主管 + 筛选规则）

```yaml
departments:
  - name: 技术部
    supervisor: {name: 张伟, title: 张总, feishu_open_id: ou_xxx, email: zhangwei@x.com}
    positions:
      - code: backend_senior
        title: 高级后端工程师
        keywords: [后端, 服务端, Python, 微服务]      # 简历没写岗位时用来归位
        screening:
          threshold: 60                               # 加权及格线
          hard_conditions:                            # 不过即淘汰
            - {field: education_level, operator: gte, value: 本科, label: 学历本科及以上}
            - {field: years_experience, operator: gte, value: 3, label: 3 年以上经验}
          weighted:                                   # 归一化到 0-100
            - {field: skills, operator: contains_any, weight: 40, value: [Python, Redis]}
```

`threshold` 演示值已按规则权重标定（技术部 60 / 算法 60 / 产品 55 / 市场 45），
保证每个部门都至少有一位候选人通过，便于演示多部门分发；HR 按实际招聘口径调整即可。
**改完保存即生效**：每轮任务开始时 `Orchestrator.sync_config()` 重新读盘并覆盖数据库快照，无需重启。

### 5.2 `prompt/screening.md`（筛选模板 / SKILL）

给 HR 看的规则说明书：字段含义、算子表、打分公式、语义匹配提示词、输出口径、修改指南。
代码里的规则执行与这份文档一一对应（`service/screening.py`）。

---

## 6. 筛选怎么算分

1. **硬性条件**：逐条判定，任一不过 → `screened_rejected`，原因写入 `hard_failed` 并留痕。
   学历按 `高中 < 大专 < 本科 < 硕士 < 博士` 排序比较，不是字符串比较。
2. **加权打分**：`Σ(命中比例 × 权重) / Σ权重 × 100`。关键词类条件命中越多分越高
   （`contains_any` 的命中比例 = 命中数 / 关键词总数），命中/未命中关键词全部记入 `score_detail`。
3. **语义匹配（可选）**：`semantic_weight > 0` 时把 JD 与简历摘要交给大模型打 0-100，
   按权重与规则分混合；语义分**不能推翻硬性条件**。
4. **阈值判定**：`score >= threshold` 且硬性全过 → `screened_passed`，按分数排序进入分发。

---

## 7. 状态机与留痕

`received → parsed → screened_passed / screened_rejected → dispatched → supervisor_feedback
→ interview_notified → (reschedule_pending → interview_notified | declined | completed)`

- `events`：每次状态变化、消息收发、筛选结果都追加一条（动作、操作者、前后状态、详情）。
- `messages`：飞书/邮件的收与发全文留存，mock 模式下就是“机器人到底发了什么”的可读副本。
- 查询：`GET /api/v1/recruitment/candidates/{id}/events`、`GET /api/v1/recruitment/events`。

---

## 8. 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/recruitment/run` | 手动触发一轮完整闭环 |
| POST | `/api/v1/recruitment/ingest` | 手动导入简历文件（pdf/docx/txt） |
| POST | `/api/v1/recruitment/ingest/text` | 手动导入简历纯文本（联调） |
| GET | `/api/v1/recruitment/candidates` | 候选人列表（状态/职位/姓名过滤） |
| GET | `/api/v1/recruitment/candidates/{id}` | 候选人详情 |
| GET | `/api/v1/recruitment/candidates/{id}/events` | 单人事件流水 |
| GET | `/api/v1/recruitment/events` | 全局事件 |
| GET | `/api/v1/recruitment/messages` | 收发留痕（`channel` / `direction`） |
| GET | `/api/v1/recruitment/positions` | 在招职位与主管映射 |
| POST | `/api/v1/feishu/webhook` | 飞书回调（`im.message.receive_v1` / `card.action.trigger`） |
| POST | `/api/v1/email/poll` | 手动触发收信 |
| POST | `/api/v1/email/test` | 邮件通道自检 |
| POST | `/api/v1/email/simulate-inbound` | 投递一封“收到的邮件”（演示/联调） |
| GET | `/health` | 健康检查（组件模式、调度状态、配置错误） |

错误统一为 `{"error": {"code", "message", "request_id", "details"}}`，响应头带 `X-Request-ID`。

---

## 9. Borrowed Implementations

> star 数与许可证均为 **2026-11-03 运行时通过 GitHub API 实读**（`stargazers_count` / `license.spdx_id`），
> 不是文档里的旧数字。文件路径均经 `raw.githubusercontent.com` 核验（核验结果见“备注”）。
> 原则：**GPL/AGPL/非标准许可的项目只借鉴设计，不复制任何代码**。

| # | 功能 | 借鉴项目 | 链接 | 借鉴的具体模块/文件/思路 | 为什么不直接使用（含许可证隔离说明） | 相比其他开源方案的优势 | 本项目对应文件 |
|---|---|---|---|---|---|---|---|
| 1 | 简历文本提取与清洗 | Unstructured-IO/unstructured（15,673★，Apache-2.0） | https://github.com/Unstructured-IO/unstructured | 借鉴 `unstructured/partition` 的分区思路：先把 PDF/DOCX/HTML 转成干净文本，再交给下游抽取（ETL 分层） | 依赖较重（含系统库），运行环境可能缺失；作为**首选可缺省依赖**，失败自动降级，未复制代码 | 对复合版面、表格、分栏的处理比裸 `pypdf` 稳 | `client/resume_parser.py` |
| 2 | 简历–职位匹配与打分 | srbhr/Resume-Matcher（29,080★，**Apache-2.0**） | https://github.com/srbhr/Resume-Matcher | 借鉴「文本提取 → 特征/关键词比对 → 打分 → 排序」的 service 层职责划分 | 它是带前端的完整应用，引入即违背“无界面”边界；只借鉴分层，不复制代码 | 流水线职责切分清晰，易于替换打分层 | `service/screening.py`、`prompt/screening.md` |
| 3 | 重复投递拦截与状态枚举 | frappe/hrms（9,014★，**GPL-3.0**） | https://github.com/frappe/hrms | **只借鉴业务逻辑**：重复投递保留首次记录、历史不丢、状态枚举简洁可辨 | GPL-3.0 有传染性，**禁止复制任何代码**；本项目自行实现去重与事件追加 | HR 领域模型成熟，状态语义贴近真实招聘 | `model/candidate.py`、`service/ingestion.py` |
| 4 | 定时任务调度 | agronholm/apscheduler（7,677★，MIT） | https://github.com/agronholm/apscheduler | 使用其 `AsyncIOScheduler` + `CronTrigger` 公共 API；锁 `>=3.11,<4`（README 明确 v4.0 为 pre-release） | 直接作为依赖使用（MIT 兼容），未改源码 | 异步原生、cron 语义完整、单进程足够本项目规模 | `factory/lifespan.py` |
| 5 | 飞书消息/事件接入 | larksuite/oapi-sdk-python（565★，MIT） | https://github.com/larksuite/oapi-sdk-python | 用官方 SDK 处理 token 管理、文件上传、事件解密；`client/feishu.py` 只做封装与归一 | 直接依赖；不自行实现签名/加密算法 | 官方维护，能力覆盖 IM 全部消息类型 | `client/feishu.py` |
| 6 | 飞书 Webhook 路由范式 | Mr-KID-github/Dify-FeiShu-bot（155★，MIT） | https://github.com/Mr-KID-github/Dify-FeiShu-bot | 借鉴「单 bot 单 `/webhook` + 挑战应答 + 事件分发到 handler」的结构；**实测其实现是 Flask（`app.py`）而非 FastAPI**，本项目用 FastAPI `APIRouter` 复刻同等范式，无代码复制 | 它是 Flask + Dify 绑定，语言栈/依赖不匹配；仅借鉴结构 | 结构极简，容易对照调试 | `api/v1/feishu.py` |
| 7 | 消息通道归一 | AstrBotDevs/AstrBot（45,746★，**AGPL-3.0**） | https://github.com/AstrBotDevs/AstrBot | 只借鉴“把各渠道事件剥离成内部消息对象再进统一管道”的架构思想 | AGPL 传染性，**零代码复制**；本项目自定义 `InboundMessage` | 多平台适配经验丰富，字段设计参考价值高 | `schema/message.py`、`client/feishu.py` |
| 8 | 通道归一（备选参考） | langbot-app/LangBot（18,701★，Apache-2.0） | https://github.com/langbot-app/LangBot | 同上，作为适配器分层的对照参考；未使用其代码 | 需要其完整运行时与依赖体系 | 适配器抽象清晰 | `schema/message.py` |
| 9 | 非阻塞邮件发送 | sabuhish/fastapi-mail（1,016★，MIT） | https://github.com/sabuhish/fastapi-mail | 借鉴 `aiosmtplib` + 后台任务的非阻塞发送模式（本项目用 `aiosmtplib` 直连 + `await`，不阻塞请求） | 本项目只需 jinja2 文本模板渲染，引入整套库收益不大；未复制代码 | 轻量、异步、依赖少 | `client/mailbox.py`、`service/notification.py` |
| 10 | 邮件线程模型 | getsentry/sentry（45,699★，GitHub API 为 **NOASSERTION**） | https://github.com/getsentry/sentry | 借鉴 `GroupEmailThread` 模型：持久化原始 `Message-ID`，后续发送设置 `In-Reply-To` / `References`（文件 `src/sentry/models/groupemailthread.py` 已核验存在） | 许可证非 OSI 标准开源（Functional Source License），**仅借鉴模型设计，禁止代码复用** | 大规模生产验证过的线程模型 | `model/email_thread.py` |
| 11 | 线程匹配与降级顺序 | zammad/zammad（6,069★，**AGPL-3.0**） | https://github.com/zammad/zammad | 借鉴「头部解析 → Message-ID 匹配 → 关联业务实体 → 兜底」的匹配顺序 | AGPL，**零代码复制**；本项目 `EmailThreadService.match` 自行实现 | 工单系统长期的邮件线程实践 | `service/email_threading.py` |
| 12 | 入站邮件轮询与回复关联 | Anil-matcha/open-agent-email（2,121★，Apache-2.0） | https://github.com/Anil-matcha/open-agent-email | 借鉴「IMAP 轮询 + 线程一致性维护 + 后台任务触发」的工程范式（仓库结构 `server/`、`client/`、`api/`） | 领域不同（通用邮件 agent），只借鉴范式 | 现代结构、部署脚本完整 | `client/mailbox.py`、`service/inbound_email.py` |
| 13 | JWZ 线程重建算法 | emersion/python-emailthreads（**49★**，MIT，已归档）·**LOW_STAR_EXCEPTION** | https://github.com/emersion/python-emailthreads | **不满足 100★ 底线，仅作算法参考**：`emailthreads/threads.py`（已核验真实路径）中的头部解析 + 树构建思路；本项目 `core/threading.py` 重写最小实现，**不引入依赖** | 已归档、star 极低，不适合做运行时依赖；重写 60 行即可满足需求 | 实现极简，是理解 JWZ 线程算法的好材料 | `core/threading.py` |
| 14 | token 化回复路由 | Flolagale/mailin（1,954★，MIT，**Node.js**） | https://github.com/Flolagale/mailin | 仅借鉴 `reply+<token>@domain` 地址别名兜底思路 | **Node.js 项目，禁止引入或移植代码**；本项目在 Python 侧自行实现 token 生成与匹配 | 兜底路由简单有效，不依赖邮件头部完整性 | `service/email_threading.py` |
| 15 | LLM 结构化抽取 | google/langextract（39,256★，Apache-2.0） | https://github.com/google/langextract | 借鉴「给定 Schema + 保留原文出处（source grounding）」的设计（`langextract/extraction.py` 已核验存在）；两类输入各一份 schema | 它面向长文档抽取管线，本项目只需一次 JSON 调用；不引入依赖 | 出处可追溯，便于人工复核 | `prompt/extraction.py`、`service/extraction.py`、`schema/extraction.py` |

---

## 10. Acceptance Checklist

验证环境：Windows + Python 3.13 + SQLite（mock 模式），2026-11-03 实跑。

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | `pytest` 全部通过 | ✅ | `81 passed in 12.67s`（`python -m pytest`） |
| 2 | `docker-compose up` 启动 app + postgres 且健康检查通过 | ⚠️ 未在本机执行 | 本机 Docker daemon 未启动（`npipe:////./pipe/dockerDesktopLinuxEngine` 连不上）；`Dockerfile`/`docker-compose.yml` 已按生产规范提供，验证命令：`docker compose up --build` → `curl localhost:8000/health`。已记录于 `DECISIONS.md` |
| 3 | `scripts/seed.py` 创建测试公司：3 部门 / 3 主管 | ✅ | 输出：技术部（张伟·张总）、产品部（李娜·李总）、市场部（王强·王总），4 个在招职位 |
| 4 | `POST /api/v1/recruitment/run` 跑通完整闭环 | ✅ | 6 份简历 → 5 通过 / 1 淘汰 → 4 个职位打包分发；`errors: []`。演示：赵敏(算法)、张明/李四(后端)、陈静(产品)、周涛(市场) 分别进对应主管 |
| 5 | 飞书 Webhook 处理 `im.message.receive_v1` 与 `card.action.trigger` | ✅ | `tests/test_feishu_events.py`（文本/富文本/@提及/卡片/未知事件/token 校验）；演示中 `handled: true, action: interview_scheduled, notices_sent: 2` |
| 6 | 邮件线程：带 `In-Reply-To` 能关联；丢头时 token 兜底能关联 | ✅ | `tests/test_email_threading.py`（References 命中、token 兜底、发件人兜底、未知发件人）；演示中候选人回信正确关联到面试通知线程 |
| 7 | LLM 抽取：飞书主管消息 ≥3 例、邮件正文 ≥3 例 | ✅ | `tests/test_extraction.py`：主管消息 3 例（含截图两种话术）、候选人回信 3 例（改期/放弃/确认），另有 LLM 接地过滤与降级用例 |
| 8 | `Borrowed Implementations` 每个功能域至少一行 | ✅ | 上表 15 行，覆盖 §4.1–§4.12 全部 12 个功能域 |
| 9 | `DECISIONS.md` 无未记录阻塞项 | ✅ | 4 个阻塞项（飞书凭证 / 邮箱账号 / Docker 未启动 / LLM 凭证）均已记录并给出 fallback |
| 10 | 仓库无任何前端文件 | ✅ | `tests/test_api_misc.py::test_repository_has_no_frontend_files` 强制校验（`.html/.css/.js/.vue/.tsx/...`） |
| 11 | `apscheduler` 版本约束 `>=3.11,<4` | ✅ | `requirements.txt` 单行约束，`test_requirements_pin_apscheduler_v3` 校验 |
| 12 | 一条命令可复现整个闭环（供人工验收） | ✅ | `python scripts/demo.py`：收到 6 / 通过 5 / 淘汰 1 → 4 个包全 `✓` → `interview_scheduled`（张明、李四，2026-11-11 14:00）→ `candidate_reschedule`（提议 11-05 15:00）→ `reschedule_confirmed` → 补发调整确认邮件 |

额外自检：`docker compose config` 语法校验、`GET /health` 实跑（`scheduler_running: true`，两个 job 已注册）、
`python scripts/demo.py` 端到端复跑（无需起服务、无需任何外部凭证）。

---

## 11. 已知限制

1. **无真实飞书凭证 / 邮箱账号**：默认 mock 模式；出站内容全量落库落盘，可逐条审阅。
2. **事件加密推送**：`live` 模式下若 `lark_oapi` 版本不提供 `AESCipher`，需在飞书后台关闭加密推送或在网关层解密（不自行实现 AES，见 D-014）。
3. **简历归位靠关键词**：简历未写岗位且关键词不足时会拒绝入库并留痕（`IngestOutcome.note`），不会瞎猜。
4. **单进程调度**：APScheduler 内存 job store，多副本部署需换成共享 job store（当前规模不需要）。
5. **建表用 `create_all`**：表结构稳定后建议引入 Alembic。

---

## 12. 测试

```bash
.venv\Scripts\python -m pytest            # 81 passed
.venv\Scripts\python -m pytest -k workflow -v
```

覆盖范围：中文时间解析（含“改到”语境优先）、筛选规则、抽取与 LLM 降级、飞书事件归一化、
邮件线程匹配、调度注册、重复投递、错误信封与请求 ID、端到端闭环（含两条主管回复路径）。
