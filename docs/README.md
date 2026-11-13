# 文档索引

本目录存放 hr-ai-robot 项目的**补充文档**（按需查阅）。根目录的 `README.md`、`AGENT.md` 是进门必读，本目录是深入细节时的参考。

## 目录结构

```
docs/
├── README.md                  # 本文件：总索引
├── 改动记录.md                # 【先看这个】按时间倒查每次改了什么、为什么、怎么验证
├── API.md                     # 所有 HTTP 接口文档
├── DECISIONS.md               # 决策台账（D-001~D-018）
│
├── design/                    # 设计与架构
│   ├── architecture.md        # 架构与各阶段时序图
│   └── innovation-statement.md # 创新点与借鉴清单
│
├── ops/                       # 部署与运维
│   ├── database-setup.md      # PostgreSQL 配置与旧库迁移
│   ├── llm-setup.md           # LLM 接入与降级顺序
│   └── unattended-protocol.md # 无人值守部署与故障处置
│
├── quality/                   # 质量与测试
│   ├── testing-report.md      # 测试覆盖报告
│   ├── known-limitations.md   # 已知限制与修复记录
│   ├── pdf-isolation-plan.md  # 损坏 PDF 隔离方案（演进方向）
│   ├── Bug修复说明_20261112.md # Boss 简历解析故障修复记录
│   └── 数据库切换PostgreSQL改动说明_20261113.md # 数据库从 SQLite 全面切 PostgreSQL 的改动台账
│
├── guide/                     # 操作指南
│   └── 最简测试指南.md         # 用真邮箱 + 真飞书实测一遍的手册
│
└── archive/                   # 归档
    └── OPTIMIZATION-NOTES.md  # 优化记录（临时交付物，验收后可删）
```

## 按角色入口

| 你是谁 | 先看什么 |
|---|---|
| **接手/评审的人** | 根目录 `README.md` → `AGENT.md` → `docs/改动记录.md` → 本目录 `API.md`、`DECISIONS.md` |
| **要知道最近改了什么** | `改动记录.md`（append-only，按时间追加） |
| **要部署上线** | `ops/`（database-setup → llm-setup → unattended-protocol） |
| **要跑测试/验收** | `quality/testing-report.md` + `guide/最简测试指南.md` |
| **想了解怎么设计的** | `design/`（architecture → innovation-statement） |
| **排查问题** | `quality/known-limitations.md` |

## 维护约定

- **每次改动都要记**：改代码 / 改配置 / 做一次性任务后，在 `docs/改动记录.md` 末尾追加一条（改了什么、为什么、改了哪些文件、怎么验证、还剩什么问题）。这是硬性约定，不是可选。
- **新增文档**：判断它属于哪一类，放进对应子目录，并在此索引登记一行。
- **功能/架构变化**：同步更新 `design/architecture.md` 和根目录 `AGENT.md` 的「文档地图」。
- **方案落地后**：从 `quality/pdf-isolation-plan.md` 这类「计划」文档，移到正式文档，计划原文进 `archive/`。
- **临时记录**：类似 `OPTIMIZATION-NOTES.md` 的临时交付物，验收通过后移入 `archive/` 或删除。
