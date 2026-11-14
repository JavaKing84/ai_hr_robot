# 05 · 配置与部署

本文件**不含任何密钥/密码/Token 取值**。凡"已设置"仅表示该键在 `.env` 中非空，长度与内容一律不记录。

---

## 1. 环境变量清单（`.env` / `.env.example` / `config/settings.py`）

读取实现：`pydantic-settings`，`env_file = <repo>/.env`、`extra="ignore"`、`case_sensitive=False`（`config/settings.py:25-30`）。即**环境变量优先于 `.env`**，未知键静默忽略。

### 1.1 运行基础

| 变量 | 用途 | 默认值 | 本机 .env | 读取位置 | 运行时生效点 |
|---|---|---|---|---|---|
| ENVIRONMENT | 环境标识，仅回显 | `dev` | `dev` | `settings.py:34` | `/health.components`、响应体 |
| DEBUG | 调试开关 | `false` | `false` | `settings.py:35` | 日志级别（`factory/lifespan.py:89`） |
| HOST / PORT | uvicorn 监听 | `0.0.0.0` / `8000` | 同默认 | `settings.py:36-37` | `core/main.py:14-15` |
| TIMEZONE | 业务时区 | `Asia/Shanghai` | 同默认 | `settings.py:38` | 调度器时区、面试时间排版、`/health.time` |
| DATABASE_URL | 数据库连接 | `postgresql+asyncpg://hr:hr@localhost:5432/hr_robot` | **已改**（用户为 `postgres`，库 `hr_robot`） | `settings.py:41` | `core/deps.py:72` 建引擎、Alembic `migrations/env.py` |
| DB_ECHO | SQL 回显 | `false` | `false` | `settings.py:42` | `build_engine(echo=...)` |

### 1.2 飞书

| 变量 | 用途 | 默认 | 本机 .env | 读取位置 | 生效点 |
|---|---|---|---|---|---|
| FEISHU_MODE | `mock`/`live` | `mock` | **live** | `settings.py:50` | `FeishuClient._sdk()` 是否真调 API（`client/feishu.py:49-65`） |
| FEISHU_APP_ID / FEISHU_APP_SECRET | 自建应用凭据 | 空 | 已设置 | `settings.py:51-52` | live 模式必需，缺失抛 `ConfigurationError` |
| FEISHU_VERIFICATION_TOKEN | 回调 token 校验 | 空 | **空** | `settings.py:53` | `client/feishu.py:412-415`：**为空则完全跳过校验**（见 §6 风险 R-1） |
| FEISHU_ENCRYPT_KEY | 回调 AES 解密 | 空 | **空** | `settings.py:54` | 收到 `encrypt` 体时才用（`client/feishu.py:409-410,418-428`） |
| FEISHU_PUSH_TARGET | `group` 群推送 / `user` 私聊 | `group` | `group` | `settings.py:62` | `feishu_group_push`（`settings.py:213-224`） |
| FEISHU_WEBHOOK_URL | 自定义机器人 webhook（仅文本） | 空 | 已设置 | `settings.py:57` | 无 `CHAT_ID` 时的文本通道（`client/feishu.py:116-122`） |
| FEISHU_CHAT_ID | 群 `oc_` 号，发文件必需 | 空 | 已设置 | `settings.py:61` | 应用身份发群消息/上传文件（`client/feishu.py:112-114,179-188`） |
| FEISHU_MENTION_OPEN_IDS | 群消息 @ 谁 | 空 | 空 | `settings.py:64` | 拼 `<at user_id=...>`（`client/feishu.py:96-102`） |
| HR_FEISHU_OPEN_ID | HR 本人 open_id | 空 | 已设置 | `settings.py:73` | ① 审批请示的接收人（`service/feedback.py:301-303`）② HR 未登记为主管时借用主管身份（`:117-118`） |
| INTERVIEW_NOTICE_REQUIRE_CONFIRM | 面试通知人工确认闸门 | `true` | `true` | `settings.py:69` | `service/notification.py:387` |
| SEMANTIC_SHADOW | 语义分影子模式 | `true` | `true` | `settings.py:80` | `service/screening.py:463-465` |

### 1.3 LLM

| 变量 | 用途 | 默认 | 本机 .env | 生效点 |
|---|---|---|---|---|
| LLM_MODE | `auto`/`live`/`off` | `auto` | **live** | `llm_enabled`（`settings.py:141-148`）：live 时**即使没有 key 也返回 True** |
| LLM_PROVIDERS | 多供应商 JSON 数组，顺序=降级顺序 | 空 | **已设置，解析出 3 家** | `llm_provider_chain`（`settings.py:150-205`）：自动补 `openai/` 前缀、裁 `/chat/completions` 后缀、去重、可选 `disable_thinking` |
| LLM_MODEL | 单供应商模型名 | `gpt-4o-mini` | `deepseek/deepseek-chat` | 无 `LLM_PROVIDERS` 时使用（`client/llm.py:130-151`） |
| OPENAI_BASE_URL | 单供应商 base | `https://api.openai.com/v1` | deepseek 域名 | 同上；**必须与 LLM_MODEL 供应商一致**（`AGENT.md:101`） |
| OPENAI_API_KEY | 单供应商 key | 空 | **空** | 同上；本机实际走 `LLM_PROVIDERS`，该键不参与 |
| LLM_FALLBACK_MODELS | 兼容旧部署的降级列表 | 空 | 空 | 同上 |
| LLM_TIMEOUT_SECONDS | 单次调用超时 | `45` | `25` | litellm/httpx timeout（`client/llm.py:198,247`） |
| LLM_DEEP_ANALYSIS | 深度解析开关 | `true` | `true` | `service/screening.py:354-356` |

### 1.4 邮件

| 变量 | 默认 | 本机 .env | 生效点 |
|---|---|---|---|
| MAIL_MODE（mock/live/off） | `mock` | **live** | `client/mailbox.py:129-133,216-220`；off = 彻底不发不收 |
| HR_MAILBOX | `hr@example.com` | 已设置（163 邮箱） | 发件人、`reply+token@域名` 的域（`client/mailbox.py:84-93`） |
| HR_SENDER_NAME | `星野科技 HR` | 同默认 | 邮件 `From` 显示名 |
| SMTP_HOST/PORT/USER/PASSWORD | 空 / 465 / 空 / 空 | 均已设置（465 + SSL） | `_live_send`（`client/mailbox.py:193-211`）；缺 `SMTP_HOST` 直接返回失败 |
| SMTP_USE_SSL / SMTP_START_TLS | `true` / `false` | `true` / 未列出（用默认） | 同上 |
| IMAP_HOST/PORT/USER/PASSWORD | 空 / 993 / 空 / 空 | 均已设置（993 + SSL） | `_live_poll`（`client/mailbox.py:263-306`） |
| IMAP_INBOX / IMAP_POLL_LIMIT | `INBOX` / `30` | 同默认 | 选箱与单轮上限 |

### 1.5 调度

| 变量 | 默认 | 本机 .env | 生效点 |
|---|---|---|---|
| SCHEDULER_ENABLED | `true` | `true` | `factory/lifespan.py:93` |
| SCHEDULE_CRON | 空（→ YAML） | `0 9 * * *` | `factory/lifespan.py:52` |
| INBOUND_POLL_SECONDS | `300` | **0** | `>0` 才注册轮询 job（`factory/lifespan.py:65`） |

---

## 2. `.env.example` 与代码读取的一致性

| 检查 | 结果 |
|---|---|
| `.env.example` 中声明的键 | 42 个，全部能在 `config/settings.py` 找到同名小写字段（`case_sensitive=False`） |
| 代码有、模板没有的键 | 无（`settings.py` 的 18 个字段全部在 `.env.example` 出现，另有派生属性不需配置） |
| 模板有、代码不读的键 | 无 |
| 注释与实际行为的偏差 | `.env.example:54-57` 说"OPENAI_BASE_URL 必须与 LLM_MODEL 一致"，实际本机用 `LLM_PROVIDERS` 绕过了这两个键；`.env.example:67` 的示例值写 `sk-xxx` 占位（正常） |

---

## 3. 容器与端口

### 3.1 `docker-compose.yml`

| 服务 | 镜像 | 端口映射 | 卷 | 健康检查 | 关键环境变量 |
|---|---|---|---|---|---|
| `postgres` | `postgres:16-alpine` | `5432:5432` | `pgdata:/var/lib/postgresql/data` | `pg_isready -U hr -d hr_robot` | `POSTGRES_USER=hr`、`POSTGRES_PASSWORD=hr`、`POSTGRES_DB=hr_robot`、`TZ=Asia/Shanghai` |
| `app` | `build: .`（`Dockerfile`） | `8000:8000` | 无（`var/` 不持久化！） | `curl -fsS http://127.0.0.1:8000/health` | `ENVIRONMENT=prod`、`DATABASE_URL` 指向 `postgres:5432`、`SCHEDULER_ENABLED=true`、其余从宿主机 env 透传 |

服务间主机名：容器内用 `postgres`（compose 服务名）；宿主机本地开发用 `localhost`。

### 3.2 `Dockerfile`

| 行 | 内容 | 说明 |
|---|---|---|
| 1 | `FROM python:3.11-slim` | 与本地 venv（3.13/3.14 混乱，见 §6 R-4）不同 |
| 5-8 | 设 `TZ=Asia/Shanghai`、不写 pyc、不缓冲输出 | |
| 12-14 | `apt-get install curl libmagic1 poppler-utils` | `unstructured` 解析 PDF 需要 |
| 16-17 | 装 `requirements.txt` | ⚠️ **快照后已变更**：`requirements.txt` 已删除，现为 `COPY pyproject.toml uv.lock` + `uv sync --frozen --no-dev`；见 `project-intel/README.md` |
| 21 | `mkdir -p var/resumes var/packages var/outbox/mail var/mock_inbox` | 运行时目录 |
| 23 | `EXPOSE 8000` | |
| 25-26 | `HEALTHCHECK curl /health` | |
| **29** | **`CMD ["python", "main.py"]`** | ⚠️ 仓库根目录**没有 `main.py`**（只有 `core/main.py`）。容器启动会 `No such file or directory`，见 §6 R-3 |

`.dockerignore` 排除 `.venv`、`var`、测试数据（`Dockerfile` 构建上下文因此不含运行时产物）。

---

## 4. 三种启动方式的准确命令

### 4.1 本地（Windows，README 推荐路径）
```bat
python -m uvicorn core.main:app --host 127.0.0.1 --port 8000
python -m alembic upgrade head
python scripts\seed.py --reset
python scripts\demo.py --verbose
curl -X POST http://127.0.0.1:8000/api/v1/recruitment/run
```
证据：`README.md:373-384`。

### 4.2 容器
```bash
docker compose up -d postgres
docker compose up --build app
```
⚠️ 上面第二条会因 `CMD python main.py` 失败（§6 R-3）。当前**能跑通**的等价写法：
```bash
docker compose run --rm app python -m uvicorn core.main:app --host 0.0.0.0 --port 8000
```

### 4.3 测试
```bash
docker compose up -d postgres        # 或用本机已有 PG
python -m pytest -q                  # 190 个用例，需要 PostgreSQL
```
测试库自动化：从 `DATABASE_URL` 派生 `<db>_test` → 不存在则 `CREATE DATABASE` → 每例 `TRUNCATE ... RESTART IDENTITY CASCADE`（`tests/conftest.py:60-127,171-178`）。可用 `TEST_DATABASE_URL` 覆盖、`TEST_PG_AUTOSTART=0` 禁止自动拉 docker。

### 4.4 本机实际拓扑（与 compose 不同，重要）
- `localhost:5432` 上跑的是名为 **`pgsql`** 的 `postgres:16-alpine` 容器（非本仓库 compose 起的 `postgres`）。
- `.env` 用 **`postgres` 超级用户** 连 `hr_robot`（compose 里则是 `hr` 账号）。因此：
  - 本机 `hr` 账号**无 CREATEDB 权限**（本次实测：用 `hr:hr` 建 `hr_robot_test_intel` 报 `InsufficientPrivilegeError: permission denied to create database`，而用 `.env` 的 `postgres` 账号探测成功）。
  - `tests/conftest.py` 的"自动建库"能力在本机只对 `postgres` 账号成立。
- `hr_robot` 库**没有 `alembic_version` 表**，即历史上是 `create_all` 建的（`scripts/init_db.py` 或应用启动）。

---

## 5. 卷、持久化与日志

| 项 | 位置 | 证据 |
|---|---|---|
| PG 数据卷 | compose 命名卷 `hr-ai-robot_pgdata` | `docker-compose.yml:13-14`（挂载点）、`:66-67`（卷声明） |
| 简历原件 | `var/resumes/`（**容器内不挂卷 → 重建容器即丢**） | `Dockerfile:20`、`config/settings.py:121-123` |
| 简历包 zip | `var/packages/<cycle_id>/` | `service/notification.py:217` |
| mock 发件 / 收件 | `var/outbox/mail/`、`var/mock_inbox/` | `client/mailbox.py:182,223` |
| IMAP 水位 | `var/imap_state.json` | `client/mailbox.py:266` |
| 应用日志 | **stdout 单行 JSON**（无文件 handler） | `core/logging.py:35-47`；容器日志用 `docker logs` |
| pytest 临时目录 | 系统 Temp（默认）/ `--basetemp=var/pytest-tmp`（文档建议） | `docs/quality/testing-report.md:73-92` |

---

## 6. 部署风险清单（按严重度）

| # | 风险 | 证据 | 影响 |
|---|---|---|---|
| R-1 | 飞书回调**无签名校验**（不校验 `X-Lark-Signature`），且 token 校验可被空值绕过（`if expected and token and token != expected`）；`FEISHU_VERIFICATION_TOKEN` 与 `FEISHU_ENCRYPT_KEY` 均未配置 | `client/feishu.py:412-415`（比较逻辑）、`:409-410`（AES 解密分支，key 为空时不可用）、本机 `.env` 两键"存在但为空" | 任何知道 URL 的人都能伪造**明文** `im.message.receive_v1` 事件，让机器人发面试邮件（当前还有 HR 确认闸门兜一层） |
| R-2 | 本机用 `postgres` 超级用户连业务库 | `.env` DATABASE_URL 用户名为 `postgres` | 最小权限原则不满足；与 compose 的 `hr` 账号语义不一致，换环境易踩权限坑 |
| R-3 | Dockerfile `CMD ["python","main.py"]` 指向不存在的文件 | `Dockerfile:27`（全文共 27 行，CMD 即最后一行），根目录无 `main.py` | `docker compose up --build` 起不来；这也是 `AGENT.md:207` 所说"Docker 链路需实跑验收"未闭环的直接原因 |
| R-4 | 本地 `.venv` 解释器与已装二进制扩展不匹配（详见 `07_TESTS_AND_GAPS.md` §1） | 本次实测：`python -V`=3.14.7，而 `asyncpg/greenlet/pydantic_core` 的 `.pyd` 均为 `cp313` | 当前状态下应用与 pytest 都无法启动；需用 Python 3.13 重建 venv 或重装依赖 |
| R-5 | compose 的 `app` 服务**完全不挂卷**（`app:` 段 `:21-64` 内没有 `volumes:`） | `docker-compose.yml:21-64` | 容器重建后简历原件、zip、IMAP 水位全丢；IMAP 水位丢失会导致**重新拉取历史邮件**（重复投递，但去重会拦住） |
| R-6 | `DATABASE_URL` 在 compose 里写死 `hr:hr`，生产口令必然改动 | `docker-compose.yml:25`（`postgresql+asyncpg://hr:hr@postgres:5432/hr_robot`）、口令定义在 `:8` | 真实部署要改成 secret 注入 |
| R-7 | `INBOUND_POLL_SECONDS=0` + 每日 cron 一次 | 本机 `.env` | 候选人回信/主管邮件最多延迟到次日 09:00 才处理（飞书反馈实时，邮件不实时） |

---

## 7. 开发 / 测试 / 生产三套配置差异

| 维度 | 开发（本机 .env） | 测试（`tests/conftest.py:184-200`） | 生产（compose app） |
|---|---|---|---|
| 飞书 | live + group | **强制 mock** | 由宿主 env 透传（默认 mock） |
| 邮件 | live（163 SMTP/IMAP） | **强制 mock**（写 `tmp_path/var/outbox`） | 由宿主 env 透传（默认 mock） |
| LLM | live + 3 家链 | **强制 off** | 由宿主 env 透传（`auto`） |
| 确认闸门 | true | **false**（闸门自身另有 `test_interview_confirm_gate.py`） | 由宿主 env 透传 |
| 调度 | 开（cron only） | 关 | `SCHEDULER_ENABLED=true` |
| 数据库 | `hr_robot` | 派生 `hr_robot_test`，每例 TRUNCATE | `postgres:5432/hr_robot`（`hr/hr`） |
| 职位配置 | `config/positions.yaml`（真实 open_id） | `tests/fixtures/positions.test.yaml`（mock open_id） | 同开发 |
| 公司名 | 星野科技（测试公司） | 同 fixtures | 同开发 |
