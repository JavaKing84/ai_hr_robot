# 数据库配置（PostgreSQL）

> 对应改动：`AGENT.md` §4-A.6(a)。**开发、测试、生产三套环境只使用 PostgreSQL**
> （2026-11-14 起 `core/db.py` 已删除 sqlite 分支，`aiosqlite` 依赖已移除，
> 非 PG 的连接串会在建引擎时直接报错）。

---

## 1. 环境分工（一句话）

| 环境 | 数据库 | 连接串来源 |
|---|---|---|
| 生产（docker-compose） | PostgreSQL | `docker-compose.yml` 内 `postgresql+asyncpg://hr:hr@postgres:5432/hr_ai_robot` |
| 本地开发 / 调试 | PostgreSQL | `.env` 或 `config/settings.py` 默认 `postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot` |
| 测试（Pytest） | PostgreSQL | `tests/conftest.py` **自动派生** `hr_ai_robot_test` 库（不存在则自动创建），不读 `_env_file` 的业务配置 |

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

- `config/settings.py` 默认：`postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot`
- `.env.example` 模板默认：同上（并带注释说明）

### 3.2 本地 `.env`（如需覆盖默认值）

```bash
DATABASE_URL=postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot
```

> 注意：`config/settings.py` 用 `pydantic-settings` 读 `.env`（`extra="ignore"`）。
> `.env` 里写了什么就连什么，没写才落到上面的默认值。连接串只能是 `postgresql+asyncpg://`，
> 其他方言会在 `build_engine` 直接抛错。

### 3.3 生产（docker-compose 内）

无需改：`docker-compose.yml` 的 app 侧本来就是
`postgresql+asyncpg://hr:hr@postgres:5432/hr_ai_robot`（容器内服务名 `postgres`，非 `localhost`）。

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

## 5. 旧 SQLite 数据（已结案，无需操作）

历史 SQLite 库的数据已在 2026-11-14 一次性搬入 PostgreSQL，那次用的迁移脚本已随之删除——
一次性任务用一次性脚本，跑完就结束，不给仓库留第二套数据库方言的痕迹。
旧库文件（`hr.db` + 两个 `.bak`）曾临时归档在 `D:/!pythonproject/_hr_ai_robot_sqlite_archive_20261114/`，
**已于 2026-11-14 确认数据完整迁入 PG 后删除**；迁移前快照仍保留在 `var/hr_robot_pgdump_before_migrate.sql`。
同期搬出的坏 venv 归档 `_hr_ai_robot_venv_broken_20261114/`（Python 3.14.7，406 MB）也已一并删除。

> 若以后真需要从别的 SQLite 库搬数据：写个一次性脚本，跑完删掉，不要把迁移代码留在 `scripts/`。

---

## 6. 边界（本项目主动不做）

- **不**改任何表结构 / ORM 模型 / 查询逻辑；
- 迁移工具**已引入 Alembic**（2026-11-13，见 `migrations/`），schema 变更一律走版本化迁移；
- **不**改 `docker-compose.yml` 的 app 侧 `DATABASE_URL`（本来就是 PG）。
