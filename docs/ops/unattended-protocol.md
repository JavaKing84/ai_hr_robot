# ai-hr 飞书招聘机器人 · 无人值守部署与运行协议

> 此文档面向接手本项目的运维/开发同事。本项目为纯后端无界面服务，基于飞书对话与邮件完成招聘闭环。
> 核心闭环：定时收简历 → AI解析/筛选 → 打包飞书发主管 → 主管飞书/邮件回复 → 自动发面试通知 → 候选人回信改期/放弃。

## 1. 环境准备

务必使用 **Python 3.11 或 3.13**，并使用独立的虚拟环境运行，避免污染系统。

```bash
# 1. 创建并激活虚拟环境
# 1. 装 uv（一次性）
winget install --id=astral-sh.uv -e
# 2. 装依赖（自动建 .venv、自动选 Python 3.12）
uv sync
```

`uv sync` 会按 `pyproject.toml` + `uv.lock` 装齐全部运行时依赖与 pytest，
**不需要额外安装任何包**；扫描件 PDF 需要更强解析时再 `uv sync --extra ocr`。

### 1.1 数据库（PostgreSQL）

本地与生产统一 PostgreSQL（详见 `docs/database-setup.md`）：

```bash
# 只起数据库容器，不起 app
docker compose up -d postgres
docker compose ps          # 应显示 healthy
```

数据库只支持 PostgreSQL（见 `docs/database-setup.md`）；旧 SQLite 数据已于 2026-11-14 一次性搬迁完毕，脚本已删除。

### 1.2 配置 `.env`

```bash
cp .env.example .env
```

必填项（其余保持默认即可运行在 mock 模式）：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 默认 `postgresql+asyncpg://hr:hr@localhost:5432/hr_robot` |
| `FEISHU_MODE` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 真实发送时设 `live` 并填自建应用凭证；未填则 `mock`（只落库留痕） |
| `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` | 飞书事件订阅校验与解密，`live` 必填 |
| `MAIL_MODE` + `SMTP_*` / `IMAP_*` | 发信与收信；`mock` 时只写 `var/outbox/mail/*.eml` |
| `LLM_MODE` / `LLM_MODEL` / `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY` | `auto` = 有 key 才调，无 key 自动回落规则抽取（离线闭环仍可跑） |

> 凭证只写进本机 `.env`（已被 `.gitignore` 忽略），**禁止提交到仓库、禁止写进日志**。

### 1.3 首次初始化

```bash
# 建表 + 同步 config/positions.yaml 里的公司与职位
.venv/Scripts/python.exe scripts/seed.py --reset

# 连通性自检（无需真实飞书/邮件凭证）
.venv/Scripts/python.exe scripts/demo.py
```

`demo.py` 跑通即表示：数据库、解析、筛选、模板渲染、mock 发信全链路可用。

---

## 2. 启动与健康检查

```bash
# 前台启动（调试用）
.venv/Scripts/python.exe main.py

# 容器方式（生产）
docker compose up -d --build
docker compose logs -f app
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","components":{"database":"postgresql+asyncpg", ...}}
```

- `status=ok` 且 `database` 为 `postgresql+asyncpg` 才算就绪；
- 反向代理 / 容器编排请把 `/health` 配成存活探针。

---

## 3. 定时任务（无人值守的核心）

| 任务 | 触发 | 配置项 |
|---|---|---|
| 每日一轮：收简历 → 筛选 → 打包发主管 | 定时（默认每天 09:00） | `SCHEDULER_ENABLED=true`、`SCHEDULE_CRON=0 9 * * *`（留空则读 `config/positions.yaml` 的 `schedule.cron`） |
| 入站邮件轮询（收主管回信 / 候选人改期） | 固定间隔 | `INBOUND_POLL_SECONDS=300` |
| 飞书事件回调 | 用户发消息时 | `POST /webhook`（需在飞书后台配置事件订阅地址） |

约束（不可改）：

- **运行时只有一个邮件轮询器**（`InboundEmailService.poll_once()`）。不要再起第二个 poller 或额外的 crontab 去调 `ingest_from_mailbox`，否则两个轮询会互吞邮件（D-003）。
- **每一轮任务里，单个职位失败不阻断其他职位**；单个候选人解析失败（含损坏 PDF）会被隔离，本轮继续。
- 服务重启后不需要补跑：下一轮按 `config/positions.yaml` 重新同步职位并继续。

---

## 4. 日常运行：看什么、在哪看

| 目录 / 接口 | 内容 |
|---|---|
| `var/logs/` | 结构化日志（JSON 行）；`grep` 关键事件即可定位一次闭环 |
| `var/outbox/mail/*.eml` | mock 模式下所有出站邮件（内容与真实发送完全一致，I-11） |
| `var/packages/` | 发给主管的简历打包结果 |
| `var/resumes/` | 收进来的简历原件（带时间戳前缀，避免重名） |
| `GET /api/v1/recruitment/candidates` | 候选人列表（含状态、分数、筛选明细） |
| `GET /api/v1/recruitment/messages?direction=outbound` | 机器人到底发了什么（审计用） |

关键日志锚点：

- `cycle.started` / `cycle.completed`：一轮的开始与结束；
- `screening.passed` / `screening.rejected`：筛选结论（含分数与硬条件失败项）；
- `interview.notice_sent` / `interview.notice_failed`：面试通知成功 / 失败（失败不重发，等人工或下轮按幂等键补发）；
- `ingest.parse_subprocess_failed`：某份简历解析子进程崩溃（已降级为空文本，需人工确认）。

---

## 5. 故障处置手册

| 现象 | 定位 | 处理 |
|---|---|---|
| 主管说「没收到飞书」 | `var/outbox` 无记录 → 飞书侧失败；有记录 → 飞书应用权限/open_id 问题 | `FEISHU_MODE=live` 且 app_id/secret 正确；主管 `feishu_open_id` 必须与飞书后台一致 |
| 主管回复了但机器人没反应 | 查 `feedback.received` 是否出现 | 没出现 → IMAP 未配置或轮询未启动；出现但 `feedback.unparsed` → 指令缺姓名/时间，机器人按 I-02 只追问不发信 |
| 面试通知发重了 | 查 `messages` 表同 `external_id` 条数 | 本项目靠 Message-ID 幂等键防重；若出现重复，先确认是否人工重发，再查 `interview.notice_sent` 次数 |
| 简历解析失败 / 服务被杀 | `ingest.parse_subprocess_failed` | 解析已隔离在子进程，本轮不受影响；把该文件转人工处理，不要在主进程里强行重试 |
| LLM 全部失败 | `llm.all_fallback_failed` | 正常现象：自动回落规则抽取，闭环不中断；检查 key 余额/网络 |
| 时间解析不对（如「下个月5号」） | `tests/test_timeparse.py` | 已被 E-01 修复覆盖；出现新的口语表达请补一条先红后绿的测试再改 `core/timeparse.py`（该文件是🔴冻结区，改动须所有者批准） |
| PG 连不上 | `/health` 的 `database` 字段 | `docker compose ps` 看容器；`.env` 的 `DATABASE_URL` 主机名在容器内应为 `postgres` 而非 `localhost` |

**升级 / 回滚**：代码回滚用 git（`git revert` / `git checkout <上一个 tag>`）；数据回滚依赖迁移前的旧库文件与 PG 备份，**升级前先 `pg_dump`**。

---

## 6. 上线前验收清单（逐条勾选）

- [ ] `.venv` 独立、Python 3.11/3.13，`pip install -r requirements-dev.txt` 无报错
- [ ] `docker compose up -d postgres` 后 `docker compose ps` 显示 healthy
- [ ] `scripts/seed.py --reset` 成功，`config/positions.yaml` 的部门/主管/职位与实际一致
- [ ] `scripts/demo.py` 完整闭环跑通（4 个职位、6 份简历、含改期链路）
- [ ] `pytest -q` 全绿（当前 167 passed，0 failed，0 skipped）
- [ ] `.env` 中 `FEISHU_MODE` / `MAIL_MODE` 按环境设为 `live`，凭证齐全且未入库
- [ ] 飞书后台事件订阅地址指向 `https://<域名>/webhook`，校验 token 与加密 key 已填
- [ ] 主管 `feishu_open_id`、邮箱与 `positions.yaml` 一致（否则打包发不出去）
- [ ] `/health` 返回 `ok`，并已接入存活探针
- [ ] 日志落盘目录有写权限、磁盘余量足够（`var/` 会持续增长）
- [ ] 已配置 `pg_dump` 定时备份

---

## 7. 变更边界（接手后请先读 `AGENT.md`）

- 状态机 10 个取值、事件动作名、模板变量名、HTTP 路径与字段、错误信封：**一律不许改**；
- 不新增依赖 / 前端 / 登录权限 / 缓存队列 / 微服务拆分；
- 测试只许新增，不许改旧断言、删测试、加 skip；
- 单次改动 ≤5 个源文件，先给计划再动手；触碰 🔴 冻结区必须所有者书面批准。

> 本协议只描述「怎么部署与运行」，不重复设计决策；决策依据见 `DECISIONS.md`，
> 施工护栏见 `AGENT.md`，已知限制见 `docs/known-limitations.md`。
