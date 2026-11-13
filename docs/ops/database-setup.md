# 数据库配置（PostgreSQL）

> 对应改动：`AGENT.md` §4-A.6(a)。**生产、本地开发、测试三套环境现在统一使用 PostgreSQL**
> （2026-11-13 起），彻底消灭「SQLite / PG」的行为差异。
> 实现文件：`core/db.py`（`build_engine` 中 `if database_url.startswith("sqlite")` 分支保留，
> 仅作为历史兼容，正常路径不会再走到）。

---

## 1. 环境分工（一句话）

| 环境 | 数据库 | 连接串来源 |
|---|---|---|
| 生产（docker-compose） | PostgreSQL | `docker-compose.yml` 内 `postgresql+asyncpg://hr:hr@postgres:5432/hr_robot` |
| 本地开发 / 调试 | PostgreSQL | `.env` 或 `config/settings.py` 默认 `postgresql+asyncpg://hr:hr@localhost:5432/hr_robot` |
| 测试（Pytest） | PostgreSQL | `tests/conftest.py` **自动派生** `hr_robot_test` 库（不存在则自动创建），不读 `_env_file` 的业务配置 |

> 测试怎么做到「零人工准备」：`tests/conftest.py` 从 `.env` 的 `DATABASE_URL` 派生
> `<库名>_test`，自动 `CREATE DATABASE`，每个用例开始前 `TRUNCATE ... RESTART IDENTITY CASCADE`，
> 等价于「每个用例一个全新的库」。PG 没启动时会自动尝试 `docker compose up -d postgres`，
> 仍失败才打印一条可直接照抄的启动命令后退出。
> 想换测试库：设环境变量 `TEST_DATABASE_URL`；想禁止自动拉起 docker：`TEST_PG_AUTOSTART=0`。

---

## 2. 本机启动 PostgreSQL

项目自带 `docker-compose.yml`，其中 `postgres` 服务已配置好用户 / 库 / 健康检查，
**二选一**（AGENT.md §4-A.6(a) 只允许这两种，不引入第三种）：

### 方式 A：Docker（推荐，无需本机装 PG）

```bash
# 在项目根目录执行：只起数据库容器，不起 app
docker compose up -d postgres

# 查看状态（应显示 healthy）
docker compose ps
```

`docker-compose.yml` 已内置：

- 镜像：`postgres:16-alpine`
- 用户 / 密码 / 库：`hr` / `hr` / `hr_robot`
- 端口：`5432:5432`
- 健康检查：`pg_isready -U hr -d hr_robot`

### 方式 B：本机已装 PostgreSQL

手动创建用户与库（库**不会**被 `create_all` 自动创建）：

```bash
psql -U postgres -c "CREATE ROLE hr LOGIN PASSWORD 'hr';"
psql -U postgres -c "CREATE DATABASE hr_robot OWNER hr;"
```

---

## 3. 配置 `DATABASE_URL`

### 3.1 默认值（已改好，无需手动设）

- `config/settings.py` 默认：`postgresql+asyncpg://hr:hr@localhost:5432/hr_robot`
- `.env.example` 模板默认：同上（并带注释说明）

### 3.2 本地 `.env`（如需覆盖默认值）

```bash
DATABASE_URL=postgresql+asyncpg://hr:hr@localhost:5432/hr_robot
```

> 注意：`config/settings.py` 用 `pydantic-settings` 读 `.env`（`extra="ignore"`）。
> 若 `.env` 里**没写** `DATABASE_URL`，则落到上面新的 PG 默认值；若写了旧 SQLite 值，
> 则以 `.env` 为准（仍会连 SQLite）。切换时请确认本地 `.env` 已改或删除该行。

### 3.3 生产（docker-compose 内）

无需改：`docker-compose.yml` 的 app 侧本来就是
`postgresql+asyncpg://hr:hr@postgres:5432/hr_robot`（容器内服务名 `postgres`，非 `localhost`）。

---

## 4. 连通性验证

```bash
# 1) 建表 + 写入测试公司（能连上 PG 即成功）
./.venv/Scripts/python.exe scripts/seed.py --reset

# 2) 完整闭环（mock 模式下无需真实飞书/邮件凭证）
./.venv/Scripts/python.exe scripts/demo.py

# 3) 健康检查里 DB 组件应显示 postgresql
curl -s http://127.0.0.1:8000/health
```

预期：`seed.py --reset` 在 PG 上建表并打印「公司：…」；`demo.py` 跑通；`/health` 的
`components.database` 为 `postgresql+asyncpg`（`database_url.split("://")[0]`）。

---

## 5. 从旧 SQLite 库迁移（`scripts/migrate_sqlite_to_pg.py`）

> 场景：之前用 `DATABASE_URL=sqlite+aiosqlite:///./var/hr_robot.db` 跑过一段时间，
> 现在切到 PG 后**历史简历 / 事件 / 消息不能丢**。这个脚本负责一次性搬运。

```bash
# 0) 先把 PG 起起来（见 §2），并确认 .env 的 DATABASE_URL 已是 postgresql+asyncpg://...
# 1) 试运行：只统计行数，不写库（可以放心反复跑）
./.venv/Scripts/python.exe scripts/migrate_sqlite_to_pg.py --dry-run
# 输出示例（本机旧库）：
#   源库（只读）: D:\!pythonproject\hr_ai_robot\var\hr_robot.db
#   目标库      : postgresql+asyncpg://hr:hr@localhost:5432/hr_robot
#   - positions      源库 4 行（dry-run，不写入）
#   ...
#   ✓ dry-run 结束，共 79 行待迁移。

# 2) 正式迁移（幂等，可重复执行）
./.venv/Scripts/python.exe scripts/migrate_sqlite_to_pg.py

# 3) 目标库已有脏数据、想清空重灌（谨慎：只 TRUNCATE 这 6 张业务表）
./.venv/Scripts/python.exe scripts/migrate_sqlite_to_pg.py --truncate

# 4) 旧库不在默认位置时指定路径
./.venv/Scripts/python.exe scripts/migrate_sqlite_to_pg.py --sqlite var/hr_robot.db
```

**脚本的四个硬约束**（都能在代码里核对）：

| 约束 | 实现 |
|---|---|
| 不改旧库 | 以 `file:...?mode=ro` 只读 URI 打开 SQLite；跑一万次也不会动源文件 |
| 保留原始 `id` | 直接插入原主键，`messages.candidate_id` / `events.object_id` 等引用不会错位 |
| 迁移后重置序列 | 灌完执行 `setval(pg_get_serial_sequence(...), MAX(id), true)`，否则下一条 INSERT 撞主键 |
| 幂等 | 目标表已有同 `id` 的行直接跳过；只有 `--truncate` 才会先清空 |

**迁移顺序**：`positions → supervisors → candidates → email_threads → messages → events`
（被引用方先写，便于人工核对；本项目未建物理外键，顺序不影响正确性）。

**类型转换**：SQLite 把时间存成字符串、JSON 存成 TEXT、布尔存成 0/1，脚本按
`Base.metadata` 的列类型逐列转换（`DateTime` → `datetime.fromisoformat`、`JSON` → `json.loads`、
`Boolean` → `bool`），避免 asyncpg 类型报错。

**迁移后自检**：

```sql
-- 1) 行数应与迁移输出一致
SELECT 'candidates' AS t, count(*) FROM candidates
UNION ALL SELECT 'events', count(*) FROM events
UNION ALL SELECT 'messages', count(*) FROM messages;

-- 2) 序列已跳过历史 id（应返回 >= MAX(id)）
SELECT last_value FROM candidates_id_seq;

-- 3) 状态机取值仍是那 10 个（附录 A，没有新值混进来）
SELECT DISTINCT status FROM candidates;
```

> 旧库文件**不要删**：迁移脚本是幂等的，留作回滚依据；确认 PG 数据可用后再手工清理。

---

## 6. 边界（本项目主动不做）

- **不**物理删除 `core/db.py` 的 sqlite 分支（历史兼容，正常路径不再使用）；
- **不**改任何表结构 / ORM 模型 / 查询逻辑；
- 迁移工具**已引入 Alembic**（2026-11-13，见 `migrations/`），schema 变更一律走版本化迁移；
- **不**改 `docker-compose.yml` 的 app 侧 `DATABASE_URL`（本来就是 PG）。
