# project-intel 目录说明

> **这是一个「快照」，不是操作手册。** 里面写的是 2026-11-14 00:53 那一刻项目长什么样。
> 要装环境、要跑服务、要跑测试，请回仓库根目录看 `README.md`。

- 生成时间：2026-11-14T00:53:52+08:00
- 对应 commit：`bf2cc81`（2026-11-13 20:58，"飞书自然语言，以及云数据库测试"）
- 用途：给人和 AI 提供当时的事实底稿（文件结构、模块职责、接口、数据模型、已知缺口），
  正文每条结论都标注了证据形式（直接证实 / 文档描述 / 运行证实 / 推断）。

保留原样而不直接改写的原因：这套文档的价值在于「当时确实是这样的」，
事后修改会让它既不像历史、也不像现状。

---

## 快照之后已经变了的事

照着下表纠正，不要照着快照原文操作。

| 快照里写的 | 现在（2026-11-14 之后）的实际情况 |
|---|---|
| 用 `requirements.txt` / `requirements-dev.txt` 装依赖 | **两个文件已删除**，改用 `pyproject.toml` + `uv.lock`；安装命令是 `uv sync`，运行是 `uv run python -m core.main` |
| `00_PROJECT_BRIEF.md` §2「声明版本（requirements.txt，直接证实）」 | 依赖清单以 `pyproject.toml` 的 `[project].dependencies` 为准 |
| `01_REPO_TREE.md` 目录树里的 `requirements.txt`、`requirements-dev.txt` | 已不存在；新增 `pyproject.toml`、`uv.lock` |
| `05_CONFIG_AND_DEPLOYMENT.md` §3.2「装 requirements.txt」 | 改为 `COPY pyproject.toml uv.lock` + `uv sync --frozen --no-dev`，`Dockerfile` 已同步改好 |
| `Dockerfile` 的 `CMD ["python", "main.py"]`（§6 R-3 记为缺陷） | 已修复为 `CMD ["uv","run","--no-dev","python","-m","core.main"]` |
| 数据库：SQLite / 本机 PostgreSQL | 统一改用 **PostgreSQL**；业务库为团队远程 `10.191.44.66:15432/hr_ai_robot`，测试库由 `tests/conftest.py` 自动派生并强制带 `_test` 后缀 |
| 迁移版本 `0001_initial` / `0002` | 现在是 `0004_message_lookup`（0003 为 JSON→jsonb、timestamp→timestamptz 加固） |
| `07_TESTS_AND_GAPS.md` 的「190 个用例」 | 现在 **222 条**（2026-11-14 实测，含新增的数据库安全护栏用例） |
| 无接口鉴权 | 已实现 `X-API-Token` / `Bearer` 鉴权，但 `API_TOKEN` 当前**留空未启用**（部署到公网前必须配置） |

---

## 以谁为准

| 想知道什么 | 看哪里 |
|---|---|
| 怎么装、怎么跑、怎么测 | 根目录 `README.md` |
| 产品边界、硬约束、纪律 | `AGENT.md` |
| 改了什么、为什么改 | `docs/改动记录.md`（只追加，每次改动必写） |
| 数据库配置与迁移 | `docs/ops/database-setup.md` |
| 接口清单与错误码 | `docs/API.md` |
| **当时的代码长什么样** | **本目录** |
