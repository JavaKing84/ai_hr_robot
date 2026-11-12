# ai-hr｜飞书 HR 招聘机器人

## 1. 项目简介

服务器上定时自动跑的招聘机器人：每天收简历 → AI 解析归位 → 按规则筛 → 按部门打包发飞书主管 → 听主管给名单和时间 → 自动给候选人发面试邮件 → 一直盯 HR 邮箱处理改期和放弃。**没有界面，界面就是飞书对话 + 邮件。**

接口见 [API.md](./API.md)，边界/配置/纪律见 [AGENT.md](./AGENT.md)，设计取舍见 [DECISIONS.md](./DECISIONS.md)。

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
├── main.py                          # 启动入口：造 app + 起 uvicorn，零业务
├── README.md                        # 本文件：流程图 + 目录 + 怎么跑
├── API.md                           # 所有 HTTP 接口：路由/入参/出参示例
├── AGENT.md                         # 边界/环境/配置/纪律/状态机（改代码前必读）
├── DECISIONS.md                     # 决策台账：为什么这么设计、哪些明确不做
├── requirements.txt                 # 运行依赖
├── requirements-dev.txt             # 测试依赖（引用 requirements.txt）
├── pytest.ini                       # pytest 配置：asyncio 自动模式
├── Dockerfile                       # 镜像：python3.11-slim + 解析器系统库
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
│   ├── settings.py                  # 读 .env，给出各种路径
│   ├── positions.yaml               # 部门/职位/主管/筛选规则
│   └── templates/                   # 六份出站文案（Jinja2）
│       ├── feishu_dispatch.md       # 给主管的简历包摘要
│       ├── resume_package_summary.md # zip 里的 00-筛选说明
│       ├── feishu_receipt.md        # 「已通知 N 人」回执
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
│   └── migrate_sqlite_to_pg.py      # SQLite 旧库整表搬到 PostgreSQL
│
├── tests/                           # 【测试】全程离线、确定性
│   ├── __init__.py                  # 空包标记
│   ├── conftest.py                  # 夹具：临时库 + 应用 + HTTP 客户端
│   ├── test_workflow.py             # 端到端：收→筛→发→回→通知→改期
│   ├── test_screening.py            # 硬性淘汰、加权打分、阈值
│   ├── test_extraction.py           # 抽取、降级、姓名接地
│   ├── test_extraction_eval.py      # 抽取评估只锁硬不变量
│   ├── test_ingestion.py            # 归位失败降级入库且不中断
│   ├── test_email_threading.py      # 邮件线程匹配与兜底
│   ├── test_timeparse.py            # 中文时间解析
│   ├── test_feishu_events.py        # 飞书事件归一化
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
│   └── testdata/                    # 【测试数据】5 份演示简历（含真实姓名，已 gitignore）
│       ├── 使用说明.md
│       ├── 01_陈昊然_高级后端工程师_通过.txt
│       ├── 02_林思远_后端开发工程师_通过.txt
│       ├── 03_赵语彤_算法工程师_通过.txt
│       ├── 04_周文彬_后端开发工程师_不通过.txt
│       ├── 05_许佳宁_后端开发工程师_不通过.txt
│       └── 格式样本/                # 同一份简历的 docx/pdf/txt
│
├── docs/                            # 【补充文档】按需查阅
│   ├── architecture.md              # 架构与各阶段时序图
│   ├── database-setup.md            # PostgreSQL 配置与旧库迁移
│   ├── llm-setup.md                 # LLM 接入与降级顺序
│   ├── unattended-protocol.md       # 无人值守部署与故障处置
│   ├── testing-report.md            # 测试覆盖报告
│   ├── known-limitations.md         # 已知限制与修复记录
│   ├── pdf-isolation-plan.md        # 损坏 PDF 隔离方案（演进方向）
│   ├── innovation-statement.md      # 创新点与借鉴清单
│   ├── OPTIMIZATION-NOTES.md        # 优化记录（验收后归档）
│   └── 最简测试指南.md              # 用真邮箱 + 真飞书实测一遍的手册
│
└── var/                             # 【运行时，已 gitignore】产物都在这
    ├── hr.db                        # 本地 SQLite 库（.env 里指到这）
    ├── hr_robot.db                  # 旧 SQLite 库（迁移前遗留）
    ├── resumes/                     # 简历原件，文件名带时间戳
    ├── packages/                    # 打好的 zip 简历包
    ├── outbox/mail/                 # mock 模式发出的 .eml
    ├── mock_inbox/                  # mock 模式收到的信
    ├── imap_state.json              # IMAP UID 水位，决定下轮从哪拉
    ├── pt-*/  pytest-*/             # pytest 临时目录（测试自动生成，可清）
    └── *.log                        # 运行/演示/构建日志
```

---

## 4. 本地运行指南

默认用 **SQLite** 就能跑通全闭环（零外部依赖，飞书/邮件/LLM 全 mock）；要用 PostgreSQL 见文末。

### 4.1 Windows

```bat
:: 1) 取代码
git clone <repo-url> hr_ai_robot
cd hr_ai_robot

:: 2) 建虚拟环境（Python >= 3.11）
py -3.11 -m venv .venv
.venv\Scripts\activate

:: 3) 装依赖
python -m pip install -U pip
python -m pip install -r requirements.txt

:: 4) 复制配置
copy .env.example .env
```

打开 `.env`，把数据库改成 SQLite（其余保持 mock 即可）：

```ini
DATABASE_URL=sqlite+aiosqlite:///./var/hr.db
FEISHU_MODE=mock
MAIL_MODE=mock
LLM_MODE=auto
SCHEDULE_CRON=0 9 * * *
```

```bat
:: 5) 建表 + 写入测试公司（3 部门 / 3 主管 / 4 职位）
python scripts\seed.py --reset

:: 6) 一条命令看完整个闭环（打印机器人真正发出的飞书文本和邮件正文）
python scripts\demo.py --verbose

:: 7) 起服务（另开一个窗口；或双击「启动简历测试.bat」）
python main.py

:: 8) 手动触发一轮
curl -X POST http://127.0.0.1:8000/api/v1/recruitment/run
```

验收：`http://127.0.0.1:8000/health` 返回 `status: ok`；`demo.py` 打印出「飞书已发 / 主管邮件已发 / 面试通知已发」即跑通。接口交互文档在 `http://127.0.0.1:8000/docs`。

> 不想开终端：依次双击 `初始化数据库.bat` → `启动简历测试.bat` → `一键跑完整闭环.bat`。

### 4.2 Linux / macOS

```bash
# 1) 取代码
git clone <repo-url> hr_ai_robot
cd hr_ai_robot

# 2) 建虚拟环境（Python >= 3.11）
python3 -m venv .venv
source .venv/bin/activate

# 3) 装依赖
python -m pip install -U pip
python -m pip install -r requirements.txt

# 4) 复制配置
cp .env.example .env
```

编辑 `.env`，把数据库改成 SQLite（Linux 可直接 sed，macOS 用 `sed -i ''`）：

```bash
sed -i 's#^DATABASE_URL=.*#DATABASE_URL=sqlite+aiosqlite:///./var/hr.db#' .env
# macOS: sed -i '' 's#^DATABASE_URL=.*#DATABASE_URL=sqlite+aiosqlite:///./var/hr.db#' .env
```

```bash
# 5) 建表 + 写入测试公司
python scripts/seed.py --reset

# 6) 一条命令跑完整个闭环
python scripts/demo.py --verbose

# 7) 起服务
python main.py
```

```bash
# 8) 另开一个终端，手动触发一轮
curl -X POST http://127.0.0.1:8000/api/v1/recruitment/run
```

验收同 Windows：`curl http://127.0.0.1:8000/health` 返回 `status: ok`。

### 4.3 换成 PostgreSQL（可选）

```bash
docker compose up -d postgres          # 只起数据库，端口 5432
# .env 改回：DATABASE_URL=postgresql+asyncpg://hr:hr@localhost:5432/hr_robot
python scripts/seed.py --reset
```

旧 SQLite 数据搬过去：`python scripts/migrate_sqlite_to_pg.py --dry-run` 先看统计，去掉 `--dry-run` 正式迁移。整包容器化：`docker compose up --build`。
