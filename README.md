# ai-hr｜飞书 HR 招聘机器人

## 1. 项目简介

服务器上定时自动跑的招聘机器人：每天收简历 → AI 解析归位 → 按规则筛 → 按部门打包发飞书主管 → 听主管给名单和时间 → 自动给候选人发面试邮件 → 一直盯 HR 邮箱处理改期和放弃。**没有界面，界面就是飞书对话 + 邮件。**

接口见 [API.md](./API.md)，边界/配置/纪律见 [AGENT.md](./AGENT.md)，设计取舍见 [DECISIONS.md](./DECISIONS.md)。

---

## 1.1 关键能力一览

| 能力 | 说明 | 开关（.env） |
|---|---|---|
| 飞书群机器人推送 | 出站消息可走群 webhook，不再逐个 open_id 私信 | `FEISHU_WEBHOOK_URL` + `FEISHU_PUSH_TARGET=group` |
| 面试通知确认闸门 | 面试通知先挂起，等 HR 回复「确认」才真正发邮件 | `INTERVIEW_NOTICE_REQUIRE_CONFIRM=true` |
| 多供应商 LLM 降级链 | 主模型失败自动按顺序换下一家，全挂则回落到规则抽取 | `LLM_PROVIDERS`（JSON 数组） |
| 语义分影子模式 + 深度解析 | 大模型打分落库留痕，但通过与否仍按规则分判定；附带 AI 亮点/风险/追问点 | `SEMANTIC_SHADOW=true`、`LLM_DEEP_ANALYSIS=true` |
| 业务接口鉴权 | 除 `/health` 与飞书回调外，所有 `/api/v1/*` 都要令牌 | `API_TOKEN`（留空=只在 dev 放行） |
| 运营视图（漏斗 / 指标） | `/ops/funnel`、`/ops/metrics`（Prometheus 文本）、`/recruitment/stats` | 无开关，随服务提供 |
| 启动自动迁移 | 起服务时在咨询锁内 `alembic upgrade head`，多副本只有一个实例执行 | `AUTO_MIGRATE=true` |
| 多副本互斥 | 每日闭环与入站轮询套 PG 咨询锁，双副本不会重复发简历包 | 无开关，自动生效 |

> 语义分权重在 `config/positions.yaml` 每个岗位的 `semantic_weight` 字段（0~100，建议 25~35）。

---

## 2. 架构流程图

### 2.1 业务闭环：双入口 × 双出口

```
                        ┌───────────────────────────────┐
                        │ 定时任务：每天 09:00 自动触发 │
                        └───────────────────────────────┘
                                        │
              ┌─────────────────────────┴─────────────────────────┐
              │                                                   │
     ┌─────────────────┐                                ┌───────────────────┐
     │ 入口 A：HR 邮箱 │                                │ 入口 B：招聘平台  │
     │ IMAP 增量拉取   │                                │ 转发邮箱 / 拖拽   │
     └─────────────────┘                                └───────────────────┘
              │                                                   │
              └─────────────────────────┬─────────────────────────┘
                                        │
                   ┌─────────────────────────────────────────┐
                   │ N0  分流：简历 / 主管回复 / 候选人回信  │
                   └─────────────────────────────────────────┘
                                        │
                                        ▼
                    ┌───────────────────────────────────────┐
                    │ N1  AI 结构化抽取              [LLM]  │
                    └───────────────────────────────────────┘
                                        │
                                        ▼
                             ┌─────────────────────┐
                             │ N2  归位职位 + 去重 │
                             └─────────────────────┘
                                        │
                                        ▼
                      ┌───────────────────────────────────┐
                      │ N3  规则筛选：硬性条件 + 加权打分 │
                      └───────────────────────────────────┘
                                        │
                                        ▼
                     ┌─────────────────────────────────────┐
                     │ N4  按职位打包 zip + 摘要 → 发主管  │
                     └─────────────────────────────────────┘
                                        │
              ┌─────────────────────────┴─────────────────────────┐
              │                                                   │
    ┌───────────────────┐                              ┌─────────────────────┐
    │ 出口 A：飞书      │                              │ 出口 B：邮箱        │
    │ 主管自由文本回复  │                              │ 在简历包原邮件回复  │
    └───────────────────┘                              └─────────────────────┘
              │                                                   │
              └─────────────────────────┬─────────────────────────┘
                                        │
                        ┌───────────────────────────────┐
                        │ N5  解析回执：名单 + 面试时间 │
                        └───────────────────────────────┘
                                        │
                                        ▼
                      ┌───────────────────────────────────┐
                      │ N6  发面试通知邮件（★ 本轮终点）  │
                      └───────────────────────────────────┘
                                        │
                                        ▼
                       ┌─────────────────────────────────┐
                       │ N7  盯回信：改期 / 放弃 / 确认  │
                       └─────────────────────────────────┘
```

### 2.2 代码分层调用

```
                 ┌─────────────────────────────────────────────┐
                 │ HTTP 请求（运维接口 / 飞书回调） + 定时任务 │
                 └─────────────────────────────────────────────┘
                                        │
                                        ▼
                 ┌─────────────────────────────────────────────┐
                 │ middlewares  请求编号 / 访问日志 / 统一报错 │
                 └─────────────────────────────────────────────┘
                                        │
                                        ▼
                   ┌─────────────────────────────────────────┐
                   │ API 层  api/v1/*.py   只收参数、注依赖  │
                   └─────────────────────────────────────────┘
                                        │
                                        ▼
                     ┌─────────────────────────────────────┐
                     │ Service 层  service/*.py   业务编排 │
                     └─────────────────────────────────────┘
                                        │
               ┌────────────────────────┴────────────────────────┐
               │                                                 │
       ┌───────────────┐                           ┌───────────────────────────┐
       │ Repository 层 │                           │ Client 层                 │
       │ 只查库 / 写库 │                           │ 飞书 / 邮件 / LLM / 解析  │
       └───────────────┘                           └───────────────────────────┘
               │                                                 │
               ▼                                                 ▼
       ┌───────────────┐                               ┌───────────────────┐
       │ Model 层      │                               │ config/ + prompt/ │
       │ 表结构与枚举  │                               │ YAML 与提示词     │
       └───────────────┘                               └───────────────────┘
```

依赖单向：`api → service → repository → model`；**所有出网动作只在 `client/`**；`core/`、`schema/` 是全层共用的底座。

---

## 3. 项目结构

```
hr_ai_robot/
│
├── README.md                        # 本文件：流程图 + 目录 + 怎么跑
├── AGENT.md                         # 边界/环境/配置/纪律/状态机（改代码前必读）
├── docs/API.md                      # 所有 HTTP 接口：路由/入参/出参示例
├── docs/DECISIONS.md                # 决策台账：为什么这么设计、哪些明确不做
├── pyproject.toml                   # 依赖与 Python 版本（唯一来源）
├── uv.lock                          # 锁定版本，`uv sync` 装出完全一致的环境
├── .python-version                  # 3.12，uv 据此选解释器
├── pytest.ini                       # pytest 配置：asyncio 自动模式
├── Dockerfile                       # 镜像：python3.12-slim + uv sync + 解析器系统库
├── docker-compose.yml               # app + postgres 一键起
├── .env.example                     # 环境变量模板，复制成 .env 再填
├── .env                             # 你本机的真实配置（不入库）
├── .gitignore                       # 排除 var/、.env、__pycache__、.idea、测试数据
├── .dockerignore                    # 排除 .venv、var、测试数据
│
├── api/                             # 【接口层】只收参数，不写业务
│   ├── __init__.py                  # 空包标记
│   └── v1/
│       ├── __init__.py              # 空包标记
│       ├── router.py                # 汇总三个子路由，前缀 /api/v1
│       ├── recruitment.py           # 触发闭环 / 导简历 / 查候选人 / 查流水
│       ├── feishu.py                # 收飞书回调，转给 feedback 解析
│       └── email.py                 # 手动收信 / 邮件自检 / 模拟一封来信
│
├── factory/                         # 【装配】把进程、中间件、路由、定时任务拼起来
│   ├── __init__.py                  # 空包标记
│   ├── app.py                       # 造 FastAPI：中间件 → 路由 → /health
│   ├── lifespan.py                  # 启动建表 + 开两个定时任务，退出清理
│   ├── middleware.py                # 中间件顺序（先日志后请求号）
│   └── routers.py                   # 只挂 /api/v1
│
├── middlewares/                     # 【横切】HTTP 出入口的检查站
│   ├── __init__.py                  # 导出中间件与异常注册
│   ├── request_id.py                # 给每个请求发唯一编号
│   ├── access_log.py                # 打印方法/路径/状态/耗时
│   └── exception.py                 # 统一报错格式，别让程序崩给调用方
│
├── schema/                          # 【数据契约】进出的数据先在这定形
│   ├── __init__.py                  # 统一导出所有 schema
│   ├── candidate.py                 # 接口的请求/响应模型
│   ├── common.py                    # 健康检查 / 错误信封 / 分页
│   ├── config.py                    # positions.yaml 的结构校验
│   ├── extraction.py                # AI 抽出来的数据结构
│   ├── message.py                   # 飞书/邮件统一成 InboundMessage
│   └── screening.py                 # 筛选结果：命中明细 + 综合分
│
├── service/                         # 【业务层】真正干活的地方
│   ├── __init__.py                  # 空包标记
│   ├── orchestrator.py              # 总指挥：串起每日闭环，单步失败不中断
│   ├── ingestion.py                 # 收简历：存文件 → 抽文字 → 入库 → 筛选
│   ├── extraction.py                # AI 解析：规则先跑，模型补全
│   ├── screening.py                 # 按规则打分淘汰
│   ├── notification.py              # 出站：打 zip、发飞书、发邮件
│   ├── feedback.py                  # 读懂主管回话：名单 + 时间
│   ├── inbound_email.py             # 收信分流：简历 / 主管 / 候选人
│   └── email_threading.py           # 把来回邮件串成一条线
│
├── model/                           # 【表结构】只描述表，不含业务
│   ├── __init__.py                  # 导入即注册全部表
│   ├── base.py                      # 时间戳混入，统一存 UTC
│   ├── candidate.py                 # 候选人表 + 10 个状态枚举
│   ├── position.py                  # 职位表（含主管与规则快照）
│   ├── supervisor.py                # 主管表：飞书 open_id + 邮箱
│   ├── message.py                   # 收发消息表：正文、解析结果、状态
│   ├── event.py                     # 事件流水表：动作、操作者、前后状态
│   └── email_thread.py              # 邮件线程表：Message-ID、reply token
│
├── repository/                      # 【数据层】只查库写库，不干别的
│   ├── __init__.py                  # 导出各 Repository
│   ├── base.py                      # 通用查询、分页、计数
│   ├── candidate.py                 # 查重（邮箱+手机）、按状态/职位查
│   ├── position.py                  # YAML → 库内快照，顺带写主管
│   ├── message.py                   # 查收发留痕
│   ├── event.py                     # 查事件流水
│   └── email_thread.py              # 按 Message-ID / token 匹配线程
│
├── client/                          # 【对外】所有网络调用只在这里
│   ├── __init__.py                  # 导出各客户端
│   ├── feishu.py                    # 飞书收发，mock/live 双模
│   ├── mailbox.py                   # SMTP 发信 + IMAP 收信
│   ├── llm.py                       # 大模型调用，失败换模型
│   └── resume_parser.py             # PDF/Word → 纯文本
│
├── prompt/                          # 【提示词】HR 与算法一起维护
│   ├── __init__.py                  # 导出提示词构造函数
│   ├── screening.md                 # 筛选规则说明书（HR 可改）
│   └── extraction.py                # 抽取提示词模板
│
├── core/                            # 【底座】跟业务无关的公共工具
│   ├── __init__.py                  # 空包标记
│   ├── main.py                      # 启动入口：造 app + 起 uvicorn，零业务（原根目录 main.py）
│   ├── db.py                        # 连库三件套：引擎/会话/基类
│   ├── deps.py                      # 容器与请求级服务装配
│   ├── exceptions.py                # 自定义报错 + 错误码
│   ├── logging.py                   # 单行 JSON 日志
│   ├── safe_process.py              # 解析放子进程，崩了不带走主程序
│   ├── threading.py                 # 邮件线程树重建
│   └── timeparse.py                 # 「下周三下午两点」→ 标准时间
│
├── config/                          # 【配置】HR 只改这里，改完下轮生效
│   ├── __init__.py                  # 空包标记
│   ├── settings.py                  # 读 .env，给出各种路径与开关
│   ├── positions.yaml               # 部门/职位/主管/筛选规则（含 semantic_weight 语义分权重）
│   ├── majors.txt                   # 专业词库（简历专业归一化用）
│   └── templates/                   # 七份出站文案（Jinja2）
│       ├── feishu_dispatch.md       # 给主管的简历包摘要
│       ├── resume_package_summary.md # zip 里的 00-筛选说明（含 AI 深度解析）
│       ├── feishu_receipt.md        # 「已通知 N 人」回执
│       ├── feishu_approval_request.md # 面试通知「待 HR 确认」审批请求
│       ├── interview_notice.md      # 面试通知邮件
│       ├── interview_update.md      # 改期确认邮件
│       └── feishu_candidate_reply.md # 候选人回信的飞书汇报
│
├── scripts/                         # 【脚本】都能单独 python scripts/xxx.py 跑
│   ├── __init__.py                  # 空包标记
│   ├── _bootstrap.py                # 给脚本补 sys.path + 造容器
│   ├── seed.py                      # 建表 + 把 YAML 写进库
│   ├── init_db.py                   # 只建表不写数据
│   ├── demo.py                      # 一条命令跑完整闭环（全 mock）
│   ├── ingest_sample.py             # 造 6 封模拟简历投进 mock_inbox
│   ├── feishu_ws_bridge.py          # 飞书长连接转本地 webhook
│   ├── eval_extraction.py           # 抽取准确率评估
│   ├── test_idempotency_demo.py     # 手动验证重复投递只留 1 条
│   ├── run_cycle_once.py            # 手动跑一轮闭环（不起服务）
│   ├── check_mail_login.py          # 检查邮箱 IMAP 登录是否正常
│   ├── interview_notices.py         # 查看/确认待发的面试通知
│   ├── e2e_four_chains.py           # 四条链路端到端自检
│   ├── probe_llm.py                 # 探测 LLM 供应商可用性
│   └── ab_semantic.py               # 语义分对照实验
│
├── tests/                           # 【测试】全程离线、确定性
│   ├── __init__.py                  # 空包标记
│   ├── conftest.py                  # 夹具：临时库 + 应用 + HTTP 客户端（测试用 fixtures/positions.test.yaml）
│   ├── fixtures/
│   │   └── positions.test.yaml      # 测试专用岗位配置（与线上 open_id 解耦）
│   ├── test_workflow.py             # 端到端：收→筛→发→回→通知→改期
│   ├── test_screening.py            # 硬性淘汰、加权打分、阈值
│   ├── test_semantic_scoring.py     # 语义分 + 深度解析
│   ├── test_extraction.py           # 抽取、降级、姓名接地
│   ├── test_extraction_eval.py      # 抽取评估只锁硬不变量
│   ├── test_ingestion.py            # 归位失败降级入库且不中断
│   ├── test_email_threading.py      # 邮件线程匹配与兜底
│   ├── test_timeparse.py            # 中文时间解析
│   ├── test_feishu_events.py        # 飞书事件归一化
│   ├── test_feishu_group_push.py    # 飞书群机器人推送
│   ├── test_interview_confirm_gate.py # 面试通知确认闸门
│   ├── test_llm_providers.py        # 多供应商 LLM 降级链
│   ├── test_scheduler.py            # 定时任务注册与触发规则
│   ├── test_safe_process.py         # 解析器崩溃主进程仍活着
│   ├── test_stability.py            # 幂等、补发、异常隔离
│   ├── test_edge_cases.py           # 缺姓名/缺时间/含糊指令
│   ├── test_api_misc.py             # 健康检查、错误信封、无前端文件
│   ├── bat/                         # 【启动脚本】双击即用
│   │   ├── 一键跑完整闭环.bat       # 双击：触发一轮完整闭环（需先起服务）
│   │   ├── 初始化数据库.bat         # 双击：建表 + 写入测试公司
│   │   ├── 启动简历测试.bat         # 双击：起服务并占住窗口
│   │   └── 启动飞书长连接.bat       # 双击：本地免公网收飞书消息
│   └── testdata/                    # 【测试数据】演示简历（含真实姓名，已 gitignore）
│       ├── 使用说明.md
│       ├── 技术部/                  # 高级后端 + 算法 + Python开发 的简历
│       ├── 产品部/                  # 产品经理简历
│       ├── 市场部/                  # 市场运营专员简历
│       └── 格式样本/                # 同一份简历的 docx/pdf/txt
│
├── docs/                            # 【补充文档】按需查阅
│   ├── README.md                    # 文档总索引
│   ├── API.md                       # 接口文档
│   ├── DECISIONS.md                 # 设计决策台账
│   ├── design/                      # 架构设计类
│   │   ├── architecture.md          # 架构与各阶段时序图
│   │   └── innovation-statement.md  # 创新点与借鉴清单
│   ├── ops/                         # 运维部署类
│   │   ├── database-setup.md        # PostgreSQL 配置与旧库迁移
│   │   ├── llm-setup.md             # LLM 接入与降级顺序
│   │   └── unattended-protocol.md   # 无人值守部署与故障处置
│   ├── quality/                     # 质量与限制类
│   │   ├── testing-report.md        # 测试覆盖报告
│   │   ├── known-limitations.md     # 已知限制与修复记录
│   │   ├── pdf-isolation-plan.md    # 损坏 PDF 隔离方案（演进方向）
│   │   └── Bug修复说明_20261112.md   # Boss 简历解析故障修复记录
│   ├── guide/                       # 使用指南类
│   │   └── 最简测试指南.md          # 用真邮箱 + 真飞书实测一遍的手册
│   └── archive/                     # 归档
│       └── OPTIMIZATION-NOTES.md    # 优化记录（验收后归档）
│
└── var/                             # 【运行时，已 gitignore】产物都在这
    ├── hr.db                        # 旧 SQLite 库（历史遗留，已不再使用，可删）
    ├── hr_robot.db                  # 旧 SQLite 库（迁移前遗留，可删）
    ├── resumes/                     # 简历原件，文件名带时间戳
    ├── packages/                    # 打好的 zip 简历包
    ├── outbox/mail/                 # mock 模式发出的 .eml
    ├── mock_inbox/                  # mock 模式收到的信
    ├── imap_state.json              # IMAP UID 水位，决定下轮从哪拉
    ├── pt-*/  pytest-*/             # pytest 临时目录（测试自动生成，可清）
    └── *.log                        # 运行/演示/构建日志
```

---

## 4. 本地开发指南

> 本章命令于 2026-11-14 在 **Windows 11 + Python 3.12.15 + uv 0.13.8 + PostgreSQL 16（Docker）** 上逐条实跑，
> 最终 206 个用例全绿。环境不一致时先看 4.8 的报错对照表。

### 4.0 三条命令跑起来

```bash
uv sync                                     # 装依赖（按 uv.lock，同一份版本）
uv run alembic upgrade head                 # 建表
uv run python -m core.main                  # 起服务 → http://127.0.0.1:8000/docs
```

就这三条。`uv sync` 会自动找到/下载 Python 3.12 并建好 `.venv`，不需要手工 `venv` + `pip install`。

想先看业务闭环、不接真实邮件（推荐）：

```bash
uv run python scripts/demo.py               # 6 份简历 → 筛选 → 分发，全程 mock
```

### 4.1 装 uv（只需一次）

Windows（PowerShell）：

```powershell
winget install --id=astral-sh.uv -e
```

装完新开一个终端，`uv --version` 有输出即可。macOS / Linux：`brew install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh | sh`。

> `uv python install 3.12` 可让 uv 自己下载解释器；`pyproject.toml` 里已写死 `requires-python = ">=3.11,<3.14"`，
> uv 不会去用 3.14（`greenlet` / `pydantic-core` 还没有稳定的 cp314 轮子）。

### 4.2 准备数据库（PostgreSQL，唯一选项）

**情况 A：用项目自带 compose（本机 5432 空闲）**

```bash
docker compose up -d postgres
```

**情况 B：复用本机已有 PostgreSQL（5432 被别的容器占用）**

```bash
docker exec -it <PG容器> psql -U postgres -c "CREATE ROLE hr LOGIN PASSWORD 'hr';"
docker exec -it <PG容器> psql -U postgres -c "CREATE DATABASE hr_ai_robot OWNER hr;"
```

项目只支持 PostgreSQL：`core/db.py` 收到非 `postgresql://` 连接串会直接抛错，没有 SQLite 分支。

### 4.3 配置 `.env`

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

通常只改一行，其余留 mock 就能跑：

```ini
DATABASE_URL=postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot
FEISHU_MODE=mock
MAIL_MODE=mock
LLM_MODE=auto
SCHEDULER_ENABLED=true
```

### 4.4 建表（Alembic）

```bash
uv run alembic upgrade head
uv run alembic current          # 应显示 0004_message_lookup (head)
```

如果这个库**以前被应用直接跑过**（启动时 `create_all` 建了表、但没有 `alembic_version`），
直接 upgrade 会报 `relation "positions" already exists`。别删库，按版本打标再升：

```bash
uv run alembic stamp 0001_initial
uv run alembic upgrade head
```

应用启动时也会自动 `upgrade head`（`AUTO_MIGRATE=true` 默认开，多副本用 PG 咨询锁互斥）。
**它只做「把库升到 head」，不做体检**：换一台机器、换一个库（比如云端）时，真正会出事的是
「版本号对上了，但 JSON 列还是 `json`、唯一索引没建」。那种情况用同步脚本，它会把三件事做完：

```bash
# 只看现状，不改任何东西（交接时先跑这个）
uv run python scripts/sync_db.py --check --target postgresql://admin:***@10.191.44.66:15432/hr_ai_robot

# 备份 → 迁移 → 逐条自证（jsonb / timestamptz / 10 个关键索引 / 幂等无重复）
uv run python scripts/sync_db.py --target postgresql://admin:***@10.191.44.66:15432/hr_ai_robot

# 云端还是空库，要把本机 6 张业务表一起搬过去（会先清空目标表，语义是「两边一致」）
uv run python scripts/sync_db.py --target postgresql://... --copy-data --yes
```

脚本的行为约定：**库里有数据就必须先备份成功，否则拒绝改库**（备份走 `pg_dump`，
本机没有时自动借用 `pgsql` 容器）；备份落在 `var/db_backup/`，同时生成两端可 diff 的索引快照。

### 4.5 灌测试数据

```bash
uv run python scripts/seed.py --reset
```

> ⚠️ `--reset` 会 drop 并重建 6 张业务表。库里有真实简历时不要加这个参数，
> 用 `uv run python scripts/seed.py` 只同步职位与主管。

### 4.6 跑通闭环（不开服务）

```bash
FEISHU_MODE=mock MAIL_MODE=mock LLM_MODE=off uv run python scripts/demo.py
```

Windows cmd 下用 `set FEISHU_MODE=mock` 三行代替；PowerShell 用 `$env:FEISHU_MODE="mock"`。
正常输出 `收到 6 份｜通过 5｜淘汰 1`，并打印主管收到的飞书文本与压缩包名。

> ⚠️ `demo.py` **不会强制 mock**，它照 `.env` 里的模式发。`.env` 是 `live` 时会真的发邮件给真人。
> 只想在现有数据上重跑就加 `--keep`。

### 4.7 起服务

```bash
uv run python -m core.main
# 或热重载开发：uv run uvicorn core.main:app --reload --port 8000
```

验证：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/v1/recruitment/positions
```

`/health` 的 `components` 会回显 feishu / mail / llm 模式、职位数、`auth` 是否开启、
入站轮询间隔，一眼看出配没配对。接口文档：`http://127.0.0.1:8000/docs`。

> ⚠️ **业务接口现在要鉴权**。`.env` 里配了 `API_TOKEN` 就必须带令牌，
> 两种写法等价：`-H "X-API-Token: $API_TOKEN"` 或 `-H "Authorization: Bearer $API_TOKEN"`；
> 不带令牌一律 401。健康检查 `/health` 和飞书回调 `/api/v1/feishu/webhook` 不需要令牌
> （后者靠飞书签名 + token 校验）。`API_TOKEN` 留空时启动会打 `security.api_token_missing`
> 警告，且只在 `ENVIRONMENT=dev` 下放行。
>
> ```bash
> curl -H "X-API-Token: $API_TOKEN" "http://127.0.0.1:8000/api/v1/recruitment/stats?days=30&daily=true"
> curl -H "X-API-Token: $API_TOKEN" "http://127.0.0.1:8000/api/v1/ops/funnel"
> curl -H "X-API-Token: $API_TOKEN" "http://127.0.0.1:8000/api/v1/ops/metrics"
> ```

### 4.8 跑测试（206 个）

测试库要先存在，且表要归跑测试的账号所有：

```bash
# 只需一次（hr 没有 CREATEDB，必须用超级用户建）
docker exec -it <PG容器> psql -U postgres -c "CREATE DATABASE hr_ai_robot_test OWNER hr;"

TEST_DATABASE_URL=postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot_test \
TEST_PG_AUTOSTART=0 uv run pytest -q
```

```text
206 passed
```

> `TEST_PG_AUTOSTART=0` 禁止 pytest 自动 `docker compose up`（本机 5432 被占用时必须关）。
> 测试会 TRUNCATE 测试库的表，`TEST_DATABASE_URL` 千万别指向有数据的库。

### 4.9 报错对照表

| 报错 | 原因 | 解决 |
|---|---|---|
| `No module named 'asyncpg.protocol.protocol'`<br>`cannot import name 'ArgsKwargs'` | venv 里解释器与二进制扩展版本不一致（在旧 venv 上重跑过 `venv`） | 删掉 `.venv` 重跑 `uv sync` |
| `ModuleNotFoundError: fastapi` | 直接用了系统 python，没走 `uv run` | 命令前加 `uv run` |
| `relation "positions" already exists` | 库是 `create_all` 建的，没有 `alembic_version` | `alembic stamp 0001_initial` 再 `upgrade head` |
| `permission denied for table xxx` | 表属于别的账号（如 postgres） | 超级用户执行 `DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION hr;` |
| `permission denied to create database` | `hr` 没有 CREATEDB | 超级用户先建 `hr_ai_robot_test`，并设 `TEST_PG_AUTOSTART=0` |
| `port is already allocated` | 5432 被其他容器占用 | 走情况 B，或改 compose 端口映射 |
| 中文路径下 `uv sync` 报错 | 项目路径含空格/中文 | 把仓库放到纯英文路径 |

### 4.10 Linux / macOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
cp .env.example .env
docker compose up -d postgres
uv run alembic upgrade head
uv run python scripts/seed.py --reset
FEISHU_MODE=mock MAIL_MODE=mock LLM_MODE=off uv run python scripts/demo.py
uv run python -m core.main
```

### 4.11 数据与文件落盘位置

| 内容 | 位置 | 说明 |
|---|---|---|
| 简历原件 | `var/resumes/` | 库里 `resume_path` 存**相对路径**（`var/resumes/xxx.pdf`），项目搬家不失效 |
| 简历包 zip | `var/packages/` | 按「部门-职位-份数-批次」命名 |
| 模拟收件箱 | `var/mock_inbox/` | mock 模式收到的邮件 |
| 发件留档 | `var/outbox/mail/` | mock 模式发出的邮件 |
| 数据库 | PostgreSQL | 6 张业务表；`candidates` 存 `resume_text`、`resume_path`、`structured`、`resume_sha256`、`parse_engine` |

原件丢失（例如项目目录被移动过）时，可从库里的文本重建副本：

```bash
uv run python scripts/rebuild_resume_files.py --dry-run
uv run python scripts/rebuild_resume_files.py
```

### 4.12 改表结构

```bash
uv run alembic revision --autogenerate -m "说明"
uv run alembic upgrade head
uv run alembic check          # 校验「模型 vs 迁移」有无漂移
```

### 4.13 常见疑问

**为什么仓库根目录没有 `main.py`？**
入口是包内的 `core/main.py`（`python -m core.main`），Dockerfile 也用它。
根目录再放一个 `main.py` 只会变成两份入口，迟早不一致。

**为什么以前 `pip install -r requirements.txt` 要装 5 分钟？**
`requirements.txt` 里有 `unstructured[pdf,docx]`，它会连带 torch / transformers / spacy，约 2GB。
现在它被移到可选 extra，`uv sync` 只装 20 来个必要包（几十秒）；真遇到扫描件 PDF 再补：

```bash
uv sync --extra ocr
```

**依赖改了以后怎么办？**

```bash
uv add httpx           # 加依赖（自动改 pyproject.toml + uv.lock）
uv remove httpx        # 删依赖
uv lock --upgrade      # 只升版本，不动 pyproject
```

`uv.lock` 要提交到仓库，别人 clone 后 `uv sync` 得到完全相同的版本。
