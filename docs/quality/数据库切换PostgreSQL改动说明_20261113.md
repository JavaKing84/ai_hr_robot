# 数据库全面切换 PostgreSQL 改动说明（2026-11-13）

> 参照《ai-hr 测试文档（2026-11-13）》的写法：结论先行、表格化、分章节、每条改动都说清「做了什么 / 为什么 / 现在什么状态」。
> 本文只讲**数据库相关改动**（含测试也切 PG、以及过程中暴露并修掉的两个真问题），不含飞书/大模型功能改动（见《ai-hr 测试文档》）。

## 0. 一句话结论

数据库从「SQLite」全面切到「PostgreSQL 16」，**生产、本地开发、测试三套环境现在全部走 PG**。
测试不再需要你手工建库、手工改配置、手工清理数据——`pytest` 直接跑，它自己建库、自己清库、PG 没起还会自己拉起容器。

**当前状态：190 个测试在 PG 上连跑 3 次全绿；四条链路验收 16/16 通过；`alembic check` 无漂移；迁移 7 表 + 7 外键全部就位。**

---

## 1. 这一轮改了哪些文件

| 文件 | 改动 | 为什么 |
|---|---|---|
| `.env` | `DATABASE_URL` → `postgresql+asyncpg://hr:hr@localhost:5432/hr_robot`；注释同步 | 运行时/生产统一指向 PG |
| `requirements.txt` | 新增 `alembic>=1.13` | 引入版本化迁移（旧实现 `create_all` 建表，改字段只能手改 SQL） |
| `alembic.ini`（新建） | Alembic 配置，`sqlalchemy.url` 留空 | URL 不写死在 ini，由 `env.py` 从 `.env` 注入，测试/生产同源 |
| `migrations/env.py`（新建） | 异步引擎接入 + 指向 `Base.metadata` | 让 autogenerate 能发现模型与库的漂移 |
| `migrations/versions/0001_initial.py`（新建） | 手写初始迁移：6 张业务表 + 7 条外键 + 唯一索引 | 数据库的「版本 1」，后续 schema 变更从这往后叠加 |
| `core/db.py` | 新增 `derive_test_database_url` / `ensure_database_exists` / `truncate_all_tables` | 把「建库、清库」做成可复用的库函数，测试和脚本共用一套，避免各写一份出错 |
| `tests/conftest.py` | **测试库从 SQLite 改为 PostgreSQL**（详见第 4 节） | 你的要求：测试也用 PG，且不用你手工改任何东西 |
| `scripts/e2e_four_chains.py` | 数据库改 PG 独立库 `hr_robot_e2e`；样例简历路径自动查找 `tests/testdata/<部门>/` | 原脚本写死 SQLite + 同学环境的 `测试简历/` 目录，在本项目跑不起来 |
| `model/candidate.py` / `message.py` / `email_thread.py` | 关联列补 `ForeignKey(..., ondelete="SET NULL")` | 删岗位/候选人不再留下孤儿数据 |
| `config/positions.yaml` | 三个主管 `feishu_open_id` 从群 `chat_id` 改回你的 `open_id` | 填错字段导致「主管识别不到」，链路③直接失败（详见第 5 节） |
| `client/resume_parser.py` | 文本提取顺序：原生库优先，`unstructured` 退为兜底 | `unstructured` 在本机会卡死 >120s，导致所有 docx/pdf 简历解析成空（详见第 5 节） |
| 文档同步 | `README.md`、`AGENT.md`、`docs/DECISIONS.md`、`docs/ops/database-setup.md`、`.env.example` | 这些文件原来都写着「测试用 SQLite」，必须一起改，否则下一个人会照着错文档操作 |

**没有动**：`model/event.py`（`object_id` 是跨表泛化关联，不适合挂外键）、`Orchestrator` 的流程与状态机、幂等逻辑——业务代码结构一行没改。

---

## 2. 新的数据库长什么样

PostgreSQL 库 `hr_robot`（`hr` / `hr` / `localhost:5432`），共 **7 张表**：

| 表 | 用途 | 外键 |
|---|---|---|
| `positions` | 岗位（部门→职位→主管） | —（被引用方） |
| `supervisors` | 主管 | —（被引用方） |
| `candidates` | 候选人 | `position_id → positions.id`（SET NULL） |
| `messages` | 消息留痕 | `candidate_id` / `position_id` / `supervisor_id`（均 SET NULL） |
| `email_threads` | 邮件线程 | 同上三条（SET NULL） |
| `events` | 事件流水 | 无（泛化关联，刻意不加） |
| `alembic_version` | 迁移版本号 | — |

**唯一索引**：`positions.code`、`supervisors.feishu_open_id`、`email_threads.token`。

**一个刻意的例外**：`messages.external_id` **没有**加唯一约束。
项目幂等策略是「应用层查重 + 失败重试时 `failed`/`sent` 两条记录可并存同 `external_id`」，
数据库加 `UNIQUE` 会直接让 3 个测试失败。所以这里保留应用层幂等。

---

## 3. 迁移怎么跑（已验证）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 起 PG | `docker compose up -d postgres` | ✅ 容器健康 |
| 建表 | `python -m alembic upgrade head` | ✅ 7 张表，版本记 `0001_initial` |
| 校验漂移 | `python -m alembic check` | ✅ `No new upgrade operations detected` |
| 灌种子 | `python scripts/seed.py --reset` | ✅ 5 岗位 + 3 主管 |
| 跑测试 | `python -m pytest -q` | ✅ 190 全绿（在 PG 上） |

过程中踩过并已修的坑（备查）：
1. 首次迁移报 `positions already exists`——迁移跑了一半但 `alembic_version` 没记上；DROP 所有表后重跑。
2. `alembic.ini` 写中文注释在 Windows 下 GBK 报错——改英文注释。
3. 迁移里 `Text`/`JSON` 列 `nullable=True` 与模型 `NOT NULL` 不符，`alembic check` 报警——改成 `nullable=False` + `server_default`。
4. 加 `UNIQUE(external_id)` 导致 3 个测试失败——撤销，保留应用层幂等（见第 2 节）。

---

## 4. 测试也用 PostgreSQL（这次的重点）

### 4.1 你要的效果

**你什么都不用做**：`python -m pytest -q` 就能跑，不需要手工建库、不需要改配置、不需要先清数据。

### 4.2 它是怎么做到的

| 环节 | 实现 |
|---|---|
| 找测试库 | 从 `.env` 的 `DATABASE_URL` 派生 `<库名>_test`（`hr_robot` → `hr_robot_test`） |
| 库不存在 | 自动连 `postgres` 维护库执行 `CREATE DATABASE`，不需要 `createdb` |
| 每个用例干净 | 用例开始前 `TRUNCATE 所有表 RESTART IDENTITY CASCADE`，等价于「每个用例一个全新的库」，自增 ID 也从 1 开始 |
| PG 没启动 | 自动执行 `docker compose up -d postgres` 并轮询到可用；仍失败才退出，并打印一条能直接照抄的命令（不是一堆堆栈） |
| 可覆盖 | `TEST_DATABASE_URL` 指定别的测试库；`TEST_PG_AUTOSTART=0` 禁止自动拉 docker（CI 用） |
| 夹具自检 | 岗位没同步进库时立即报「夹具异常」，而不是让业务输出「没有在招职位」误导排查方向 |

### 4.3 为什么这样最不容易出错（对比）

| 方案 | 做法 | 结论 |
|---|---|---|
| 每用例建库 | `CREATE DATABASE` × 190 次 | 太慢（每次数百毫秒），还容易撞名 → 不采用 |
| 每用例建 schema | 每个用例独立 schema + search_path | 要改连接参数、asyncpg 下易出坑 → 不采用 |
| **一库 + 每用例清空**（采用） | `TRUNCATE ... RESTART IDENTITY CASCADE` | 最快、语义等价、无并发污染（pytest 默认串行） |
| 测试仍用 SQLite | 旧做法 | 与生产不同引擎，PG 独有的字段长度/JSON/外键问题测不出来 → 已废弃 |

> 注意：这套清库方案依赖「测试串行执行」。以后若引入 `pytest-xdist` 并行，需要改成每 worker 一个独立库，届时会一并调整。

### 4.4 验证结果

- 190 个用例在 PG 上**连续跑 3 次全绿**；
- 删库后从零首跑也全绿（自动建库路径已验证）；
- 首轮改造时曾出现一次偶发失败（岗位表为空导致判定「配置为空」），**未能复现**；已加夹具自检，若再出现会直接定位到夹具而不是业务。

---

## 5. 顺带修掉的两个真问题（都是原来就有的）

### 5.1 docx / pdf 简历解析成空文本（影响真实投递）

**现象**：链路②（BOSS 直聘转发 docx 附件）被判 0 分、「硕士及以上学历不符」淘汰，库里 `resume_text` 长度为 0。

**根因**：解析器原本**优先**调用 `unstructured`。本机实测它的 `partition()` 处理 docx **超过 120 秒不返回**（导入本身只要 3.9 秒，疑似联网取模型），而解析跑在 60 秒超时的子进程里 → 被强杀 → 降级成空文本 → 抽取不到学历 → 硬条件淘汰。等于**所有 docx/pdf 简历都会被误杀**。

**修复**：改成「先快后全」——`.docx` 先用 `python-docx`、`.pdf` 先用 `pypdf`；只有它们一个字都没抽到时，才用 `unstructured` 兜底。

**实测**：`陈昊然-简历.docx` 0.14 秒 / 403 字（原 >120 秒超时）；`陈昊然-简历.pdf` 0.14 秒 / 430 字。

### 5.2 主管 `feishu_open_id` 被填成群 `chat_id`

**现象**：链路③ HR 在飞书说「这 2 份我只留了…请通知下周三下午 14:00 面试」，返回 `action=supervisor_unknown`，一份通知都挂不起来。

**根因**：`config/positions.yaml` 里三个主管的 `feishu_open_id` 全是 `oc_8bb94bc8a2a75ebe705e86968c916d67`——那是**群 chat_id**，不是个人 `open_id`（个人是 `ou_` 开头）。所以按 open_id 反查主管永远查不到。

**修复**：三个主管统一改回你的 `ou_158953a5c9f55549940acecacd7e47a0`（与 `.env` 的 `HR_FEISHU_OPEN_ID` 一致），并更新文件头注释说明两者区别。

**影响面**：不修的话，飞书里「认人」（主管身份识别）、面试通知确认的审批人识别、未归位告警的收件人都不可用。群推送走 webhook + chat_id，不受影响，所以之前一直没被发现。

---

## 6. 目前还剩哪些问题

| # | 问题 | 状态 | 说明 |
|---|---|---|---|
| 1 | 飞书 `im:resource:upload` 权限未开 | ❌ 未解决 | 简历压缩包进不了群（`99991672`），需在开放平台点一下开通 |
| 2 | DeepSeek 兜底 key 是否生效 | ⚠️ 待复测 | 你已换 key，本轮未跑 `probe_llm.py` 复测 |
| 3 | `unstructured` 在本机会卡死 | ⚠️ 已绕过 | 已降为兜底，正常简历不会再走到；但「原生库抽不出文本」的文件仍会白等一次 60s 超时 |
| 4 | `resume_text` 明文存库、未脱敏 | ⚠️ 未处理 | 个人信息合规项，属后续优化 |
| 5 | `events` 表只增不改 | ⚠️ 未处理 | 后续归档/分区，不影响当前功能 |
| 6 | 清库方案依赖测试串行 | ℹ️ 已知约束 | 引入 `pytest-xdist` 时需调整（见 4.3） |
| 7 | `var/hr.db`、`var/hr_robot.db` 旧 SQLite 文件仍在 | ℹ️ 可删 | 已不再使用，按你之前的意思直接删即可 |

---

## 7. 怎么复跑

```bash
# 1) 起 PostgreSQL（需 Docker Desktop）
docker compose up -d postgres

# 2) 建表 / 升级到最新
.venv\Scripts\python.exe -m alembic upgrade head

# 3) 校验「模型 vs 迁移」无漂移
.venv\Scripts\python.exe -m alembic check

# 4) 跑测试（自动建 hr_robot_test、自动清库）
.venv\Scripts\python.exe -m pytest -q

# 5) 四条链路验收（邮件 mock，不发给真人；PG 独立库 hr_robot_e2e）
.venv\Scripts\python.exe scripts\e2e_four_chains.py

# 6) 模型链路体检（顺便复测 DeepSeek 新 key）
.venv\Scripts\python.exe scripts\probe_llm.py
```

> `e2e_four_chains.py` 默认按 `.env` 的 `FEISHU_MODE=live` 往真实群推送；只想验证流程不打扰群，用
> `set FEISHU_MODE=mock` 再跑（PowerShell 用 `$env:FEISHU_MODE="mock"`）。
