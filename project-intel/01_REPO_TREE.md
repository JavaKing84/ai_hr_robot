# 01 · 仓库结构与文件职责

扫描 commit：`bf2cc81`（branch `develop`），生成于 2026-11-14T00:53:52+08:00。
排除：`.venv/`、`.git/`、`.pytest_cache/`、`.idea/`、`var/`（gitignore 的运行时产物）、`tests/testdata/`（未跟踪）、`__pycache__`。

---

## 1. 关键目录树（含真实职责）

```
hr_ai_robot/
├── README.md                  # 产品闭环 ASCII 图 + 目录说明 + 本地运行/测试/Alembic 命令
├── AGENT.md                   # 给人和 AI 的硬约束：产品边界、环境、配置、五条判定规则、状态机、已知限制
├── Dockerfile                 # python:3.11-slim + curl/libmagic1/poppler-utils；CMD 指向不存在的 main.py（见差异 D-1）
├── docker-compose.yml         # app + postgres 两个服务；app 用 ENVIRONMENT=prod 与 postgres 主机名注入环境变量
├── alembic.ini                # url 留空，运行时由 migrations/env.py 从 Settings.DATABASE_URL 注入
├── pytest.ini                 # asyncio_mode=auto；testpaths=tests
├── requirements.txt           # 运行依赖（含 litellm、unstructured、lark-oapi）
├── requirements-dev.txt       # -r requirements.txt + pytest/pytest-asyncio
├── .env.example               # 全部环境变量模板（注释里写明 mock/live/off 与供应商对应关系）
├── .gitignore                 # 忽略 .venv/ var/ .env .idea/ tests/testdata/ audit_output/
│
├── api/                       # 【接口层】只解析参数 + 注入 Services（core/deps.py），不写业务
│   └── v1/
│       ├── router.py          # 聚合 recruitment/feishu/email 三个子路由（前缀 /api/v1 由 factory/routers.py 加）
│       ├── recruitment.py     # 触发闭环 / 文件&文本导入简历 / 查候选人·事件·消息·职位
│       ├── feishu.py          # POST /feishu/webhook：解码 + 挑战应答 + normalize_event → feedback.handle()
│       └── email.py           # 手动收信 / 发通道自检邮件 / 模拟一封入站邮件（联调用）
│
├── factory/                   # 【装配】进程级拼装，不含业务
│   ├── app.py                 # create_app()：装中间件→路由→/health；健康检查里带 DB/飞书/邮件/LLM/配置状态
│   ├── lifespan.py            # 启动 init_models() + 注册 APScheduler 两个 job；退出 shutdown + dispose engine
│   ├── middleware.py          # 注册顺序：先 AccessLog 后 RequestId（后者更外层，日志才拿得到 request_id）
│   └── routers.py             # include_router(api_router, prefix="/api/v1")
│
├── middlewares/               # 【横切】
│   ├── request_id.py          # 每请求生成/透传 request id
│   ├── access_log.py          # 方法/路径/状态/耗时单行日志
│   └── exception.py           # 统一错误信封（core/exceptions.py 的业务异常 → JSON）
│
├── core/                      # 【底座】业务无关工具
│   ├── main.py                # 进程入口：app = create_app()；uvicorn.run(app, log_config=None)
│   ├── db.py                  # Base / build_engine / session_factory / init_models(create_all) / 测试库派生·建库·TRUNCATE
│   ├── deps.py                # Container（进程级单例）+ build_services(session)（请求/任务级）+ FastAPI 依赖
│   ├── exceptions.py          # 业务异常与错误码（ConfigurationError / ValidationFailed / NotFoundError / LLMUnavailable ...）
│   ├── logging.py             # 单行 JSON 日志 + log_extra()
│   ├── safe_process.py        # run_in_subprocess()：把可能段错误/卡死的解析器放进 spawn 子进程，超时 terminate→kill
│   ├── threading.py           # Message-ID 归一化、References 头解析
│   └── timeparse.py           # 「下周三下午两点」→ aware datetime；naive UTC 与本地时间互转；面试时间排版
│
├── config/                    # 【配置】HR 唯一需要改的地方
│   ├── settings.py            # pydantic-settings 读 .env；派生路径；LLM 供应商链解析；群推送判定
│   ├── positions.yaml         # 公司/部门/主管（open_id + email）/职位（keywords + screening 规则）
│   ├── majors.txt             # 专业词库（178 行，简历专业归一化白名单）
│   └── templates/             # 7 份 Jinja2 出站文案（StrictUndefined，缺变量直接报错）
│       ├── feishu_dispatch.md            # 给主管的简历包摘要
│       ├── resume_package_summary.md     # zip 内 00-筛选说明.md（含 AI 深度解析三段）
│       ├── feishu_receipt.md             # 「已通知 N 人」回执
│       ├── feishu_approval_request.md    # 面试通知「待 HR 确认」请示
│       ├── interview_notice.md           # 面试通知邮件
│       ├── interview_update.md           # 改期确认邮件
│       └── feishu_candidate_reply.md     # 候选人回信的飞书汇报
│
├── model/                     # 【表结构】只有列定义与常量，无业务逻辑
│   ├── base.py                # TimestampMixin（created_at/updated_at，naive UTC）+ utcnow_naive()
│   ├── candidate.py           # Candidate + CandidateStatus（11 个取值 + LABELS 中文标签）
│   ├── position.py            # Position（YAML 运行期快照：keywords/threshold/screening JSON/主管四字段/active）
│   ├── supervisor.py          # Supervisor（feishu_open_id 唯一 + email）
│   ├── message.py             # Message（channel/direction/正文/external_id/thread_key/parsed/status/raw）
│   ├── event.py               # Event（action/actor/object_type/object_id/from_state/to_state/detail/note）
│   └── email_thread.py        # EmailThread（token/kind/root_message_id/message_ids/message_id_index/关联三 ID）
│
├── repository/                # 【数据层】单聚合根查询，禁止出网
│   ├── base.py                # get/add/add_all/count
│   ├── candidate.py           # find_duplicate（邮箱 OR 手机号）/ list_by_position / list_by_cycle / build_search+search
│   ├── position.py            # sync_from_config（YAML→库 upsert，删除的职位置 active=false）+ SupervisorRepository
│   ├── message.py             # find_delivered（按 external_id）/ find_delivered_by_content（飞书内容级幂等）/ list_failed_outbound
│   ├── email_thread.py        # create / find_by_token / find_by_any_message_id / find_for_candidate / append_message_id
│   └── event.py               # record / list_for_object / list_recent / list_since
│
├── service/                   # 【业务层】
│   ├── orchestrator.py        # 每日闭环总编排 + sync_config + poll_inbound（单步失败不阻断）
│   ├── ingestion.py           # 字节→落盘→解析→抽取→归位→去重→入库→调筛选；未归位降级入库 + 飞书告警
│   ├── extraction.py          # 三类抽取统一管道（简历 / 面试安排 / 候选人回信），规则与 LLM 按字段合并
│   ├── screening.py           # evaluate() 纯函数打分 + ScreeningService 落库留痕 + 语义分/深度解析
│   ├── notification.py        # 出站：打 zip、飞书文本/文件、主管邮件副本、面试通知、改期确认、请示、补发
│   ├── feedback.py            # 主管反馈唯一用例入口：审批闸门 / 自然语言路由 / 淘汰 / 改期 / 排期
│   ├── inbound_email.py       # 一次轮询 + 四类分流（简历优先于「按发件人认人」）
│   ├── email_threading.py     # 线程匹配五级兜底 + 平台发件人/带简历附件例外 + 线程补登记
│   └── agent.py               # 飞书自然语言助手：路由（模型只判意图）→ 事实检索 → 组句，写操作不碰库
│
├── client/                    # 【对外】所有出网动作只在这里
│   ├── feishu.py              # 官方 SDK 封装：群文本/群文件/私聊文本/私聊文件/解密/事件归一/下载资源
│   ├── mailbox.py             # SMTP 发送（aiosmtplib）+ IMAP 轮询（UID 水位）+ mock 收发 + RFC822 解析
│   ├── llm.py                 # litellm 多供应商降级链；httpx 直连兜底；JSON 抽取容错；complete_text
│   └── resume_parser.py       # PDF/DOCX/TXT → 纯文本（unstructured→pypdf→python-docx）+ 规则 prefill 预填
│
├── prompt/                    # 【提示词】与 screening.py 一一对应
│   ├── extraction.py          # RESUME/INTERVIEW/REPLY/SEMANTIC/DEEP_ANALYSIS 五套 system + few-shot + 构造函数
│   ├── agent.py               # ROUTER_SYSTEM（意图路由）+ ANSWER_SYSTEM（基于事实作答）+ 构造函数
│   └── screening.md           # HR 可读的筛选规则说明书（算子表、打分公式、语义提示词原文）
│
├── schemas → schema/          # 【数据契约】Pydantic v2
│   ├── candidate.py           # 接口请求/响应（CandidateRead/RunCycleResponse/IngestResponse/EmailPollResponse...）
│   ├── common.py              # HealthResponse / 错误信封 / 分页
│   ├── config.py              # positions.yaml 结构校验（HardCondition/WeightedCriterion/ScreeningConfig/CompanyConfig）
│   ├── extraction.py          # ExtractedCandidate / InterviewScheduleSchema / CandidateReplySchema / SourceQuote
│   ├── message.py             # InboundMessage（飞书+邮件归一）
│   ├── screening.py           # ScreeningResult / CriterionHit / HardConditionResult
│   └── agent.py               # AgentIntent / AgentPlan / AgentFacts / normalize_intent
│
├── migrations/                # Alembic
│   ├── env.py                 # 从 config.settings 注入 DATABASE_URL（异步 asyncpg）
│   └── versions/0001_initial.py  # 6 张业务表 + 外键 + 索引（唯一一版迁移）
│
├── scripts/                   # 【可独立执行的脚本】均通过 _bootstrap.py 补 sys.path 并造容器
│   ├── _bootstrap.py          # 路径与容器引导
│   ├── seed.py                # 建表 + 把 YAML 写进库（--reset 清空）
│   ├── init_db.py             # 只建表
│   ├── demo.py                # 全 mock 跑完整闭环并打印真实外发文本
│   ├── ingest_sample.py       # 造 6 封模拟简历投进 mock_inbox
│   ├── run_cycle_once.py      # 不起服务跑一轮
│   ├── e2e_four_chains.py     # 四条链路端到端自检
│   ├── interview_notices.py   # 列出/确认挂起的面试通知（人工确认闸门的 CLI 入口）
│   ├── check_mail_login.py    # IMAP/SMTP 登录自检（只连不发）
│   ├── feishu_ws_bridge.py    # 飞书长连接 → 本地 webhook 转发
│   ├── eval_extraction.py     # 抽取量化评估（few-shot/测试集/准确率）
│   ├── ab_semantic.py         # 语义分 A/B 对照
│   ├── probe_llm.py           # 逐家探测 LLM 供应商可用性
│   ├── fetch_resume.py        # 按候选人取回简历原件
│   ├── migrate_sqlite_to_pg.py# 旧 SQLite 整表迁 PostgreSQL
│   └── test_idempotency_demo.py # 纯标准库验证重复投递只留 1 条
│
├── tests/                     # 【测试】190 个用例，全程离线（DB 用真 PG）
│   ├── conftest.py            # 夹具：派生 hr_robot_test、自动建库/建表/每例 TRUNCATE、mock 三件套、HTTP client
│   ├── fixtures/positions.test.yaml  # 测试专用职位配置（与线上 open_id 解耦）
│   ├── test_workflow.py(5) test_ingestion.py(4) test_screening.py(8) test_semantic_scoring.py(9)
│   ├── test_extraction.py(16) test_extraction_eval.py(52) test_email_threading.py(8)
│   ├── test_timeparse.py(28) test_feishu_events.py(9) test_feishu_group_push.py(4)
│   ├── test_interview_confirm_gate.py(5) test_llm_providers.py(5) test_scheduler.py(6)
│   ├── test_safe_process.py(5) test_stability.py(12) test_edge_cases.py(7) test_api_misc.py(7)
│   └── bat/                   # 10 个双击即用的 .bat（初始化库/起服务/跑闭环/确认通知/查待确认/长连接...）
│
├── docs/                      # 【补充文档】17 篇，2718 行
│   ├── README.md  API.md(429)  DECISIONS.md(113)  改动记录.md(114)
│   ├── design/{architecture.md(233), innovation-statement.md(84)}
│   ├── ops/{database-setup.md(167), llm-setup.md(187), unattended-protocol.md(166)}
│   ├── quality/{testing-report.md(96), known-limitations.md(84), pdf-isolation-plan.md(93), Bug修复说明_20261112.md(158), 数据库切换PostgreSQL改动说明_20261113.md(173)}
│   ├── guide/{最简测试指南.md(162), 飞书自然语言助手.md(123)}
│   └── archive/OPTIMIZATION-NOTES.md(282)
│
├── audit_output/              # 历史审计产物（gitignore），非运行时依赖
└── var/                       # 【运行时，gitignore】resumes/ packages/ outbox/mail/ mock_inbox/ imap_state.json + 历史修复备份 fix_backup_2026111x/
```

---

## 2. 重要入口文件（按"从外到内"排序）

| 顺序 | 文件:行 | 作用 |
|---|---|---|
| 1 | `core/main.py:10` | 进程内唯一的 `app` 实例；`python -m core.main` 可直接起服务 |
| 2 | `factory/app.py:31` | `create_app()`：注入 Container、注册中间件/路由/`/health` |
| 3 | `factory/lifespan.py:85` | 生命周期：建表 → 起调度器 → 退出清理 |
| 4 | `factory/routers.py:11` | 唯一路由挂载点 `/api/v1` |
| 5 | `core/deps.py:121` | `build_services()`：一个 session 内装配 8 个 service（依赖注入的真正枢纽） |
| 6 | `service/orchestrator.py:73` | 每日闭环唯一入口 |
| 7 | `service/inbound_email.py:109` | 所有入站流量的分流闸门 |
| 8 | `service/feedback.py:112` | 所有主管/HR 反馈的唯一用例入口（飞书与邮件共用） |
| 9 | `service/notification.py:87` | 所有出站动作的编排起点 |

---

## 3. 关于目录的两点事实校正

1. 目录里是 `schema/`（单数），文档正文多处写作 `schemas`；以 `schema/` 为准。
2. `var/` 内存在 `fix_backup_20261112b~e/` 四个历史备份目录（`ingestion.py`、`notification.py`、`settings.py` 等旧副本）与两个旧 SQLite 库、一份 `hr_robot_pgdump_before_migrate.sql`。这些**不参与运行**，但会干扰 `grep`（例如搜索 `duplicate_of_id` 会命中备份）。本报告已把 `var/` 排除在事实依据之外。
