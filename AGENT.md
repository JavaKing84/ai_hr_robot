# AGENT.md｜边界、环境、配置与开发纪律

> 给接手的人和 AI 看的硬约束。改代码前先读完这一页；想加功能先看 §1 和 §7。

---

## 1. 产品边界（硬性）

**只做这七件事**：定时收简历（邮箱 / 平台）→ AI 解析分类 → 规则筛选 → 打包发飞书主管 → 收主管回执（飞书 / 邮箱）→ 发邮件通知候选人 → 监控 HR 邮箱处理改期 / 放弃，并留痕。

**绝对不做**（不是遗漏，是边界；`tests/test_api_misc.py` 会校验仓库里没有前端文件）：

| 看起来该做 | 为什么不做 |
|---|---|
| Web / 小程序 / APP 任何界面 | 产品定位是机器人，界面即飞书对话与邮件 |
| 面试官角色、一面/二面、面试评价 | 没有面试官这个概念 |
| 笔试、测评、第三方招聘系统对接 | 范围外 |
| 管理员、人才库共享、候选人自助改期页 | 不需要；候选人只收发邮件 |
| 被淘汰者的感谢信 | 只记 `screened_rejected` 状态与事件 |
| 固定次数重试 / 退避重试 | 会给候选人连发多封邮件；失败走「下一轮按幂等键补发」 |
| 自动补发改期确认（不经主管） | 改期必须主管点头，见 §5 规则 4 |
| 为「未归位」新增状态枚举 | 状态机 10 个枚举冻结，未归位者停在 `received` |

---

## 2. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **>= 3.11** | 用到 `X \| None` 等新语法；镜像 `python:3.11-slim` |
| 数据库 | **PostgreSQL**（本地 / 测试 / 生产统一） | 生产 `postgresql+asyncpg://hr:hr@postgres:5432/hr_robot`；测试自动派生 `hr_robot_test` 库（`tests/conftest.py`，不存在则自动建、每个用例前清空） |
| 飞书 | 自建应用 | 需 `im:message`、上传/获取文件资源、通过手机号或邮箱获取用户 ID；订阅 `im.message.receive_v1`；版本已发布且在可用范围内 |
| 邮箱 | 支持 **IMAP + SMTP** | 建议专职 HR 邮箱；SSL 常用 993 / 465 |
| 大模型 | OpenAI 兼容接口（经 litellm） | 无 key 时自动走确定性规则抽取，闭环仍可跑 |
| 系统库（容器内） | `curl`、`libmagic1`、`poppler-utils` | 供健康检查与 PDF/DOCX 解析 |

三种外部依赖都是三态开关，**mock 不是假数据分支**：出站只写库/磁盘，入站仍是真实报文格式。

| 变量 | 取值 | 默认 | 含义 |
|---|---|---|---|
| `FEISHU_MODE` | `mock` / `live` | `mock` | 是否真调飞书 API |
| `MAIL_MODE` | `mock` / `live` / `off` | `mock` | `off` 时彻底不发不收 |
| `LLM_MODE` | `auto` / `live` / `off` | `auto` | `auto` = 有 `OPENAI_API_KEY` 才调 |

---

## 3. 配置说明

### 3.1 `config/positions.yaml` — HR 唯一必改的文件

结构：`company` / `schedule` / `mailbox` / `departments[] → supervisor + positions[]`。

```yaml
departments:
  - name: 技术部
    supervisor: {name: 张伟, title: 张总, feishu_open_id: ou_xxx, email: zhangwei@x.com}
    positions:
      - code: backend_senior
        title: 高级后端工程师
        headcount: 2
        keywords: [后端, 服务端, Python, 微服务]   # 简历没写岗位时用来归位
        screening:
          threshold: 60                            # 加权及格线
          rule_version: v1
          hard_conditions:                         # 任一不过即淘汰
            - {field: education_level, operator: gte, value: 本科, label: 学历本科及以上}
          weighted:                                # 归一化到 0-100
            - {field: skills, operator: contains_any, weight: 40, value: [Python, Redis]}
```

| 字段 | 作用 |
|---|---|
| `supervisor.feishu_open_id` | 简历包飞书发给谁（必填，否则发不出去） |
| `supervisor.email` | 简历包邮件副本收件人；**没有这封邮件，主管就没法「在原邮件上回复」** |
| `positions[].keywords` | 简历没写岗位时的归位依据 |
| `screening.threshold` | 加权分及格线 |
| `screening.hard_conditions[]` | 硬性条件，`operator` 支持 `gte` / `lte` / `eq` / `ne` / `in` / `not_in` / `contains_any` / `contains_all` / `exists` |
| `screening.weighted[]` | 加权项，`weight` 之和不必等于 100，按比例归一化 |
| `schedule.cron` | 每日触发时间（5 段 cron） |
| `mailbox.interview_location` | 面试地点，写进通知邮件 |

**保存即生效**：每轮任务开头 `sync_config()` 重读磁盘覆盖数据库快照，无需重启。YAML 里删掉的职位置 `active=false`，不物理删除（保留历史简历关联）。

### 3.2 `prompt/screening.md` — 筛选模板（SKILL）

给 HR 校对和迭代的规则说明书：字段含义、算子表、打分公式、语义匹配提示词、输出口径。**与 `service/screening.py` 一一对应，改规则要两边一起改。**

打分公式：`Σ(命中比例 × 权重) / Σ权重 × 100`；`semantic_weight > 0` 时把 JD 与简历摘要交给大模型打 0–100 分混合。**语义分不能推翻硬性条件。**

### 3.3 `.env` — 账号密码与运行参数

基础模板见 `.env.example`，关键项：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://hr:hr@localhost:5432/hr_robot` 或 `sqlite+aiosqlite:///./var/hr.db` |
| `HR_MAILBOX` / `HR_SENDER_NAME` | 发件地址与显示名 |
| `SMTP_*` / `IMAP_*` | 收发服务器与账号密码 |
| `FEISHU_APP_ID` / `SECRET` / `VERIFICATION_TOKEN` / `ENCRYPT_KEY` | 自建应用凭据 |
| `LLM_MODEL` | 主模型，如 `deepseek/deepseek-chat` |
| `OPENAI_BASE_URL` | **必须与 `LLM_MODEL` 的供应商一致**，否则 401/502 |
| `OPENAI_API_KEY` | key 统一写这里（写 `DEEPSEEK_API_KEY` 不会被读） |
| `LLM_FALLBACK_MODELS` | 降级候选，逗号分隔，须与主模型同一供应商 |
| `LLM_TIMEOUT_SECONDS` | 单次调用超时，默认 45 秒 |
| `SCHEDULE_CRON` | 留空则读 `positions.yaml` 的 `schedule.cron` |
| `INBOUND_POLL_SECONDS` | 入站邮件轮询秒数，`0` 关闭；测试期建议 `0` |
| `SCHEDULER_ENABLED` | `false` 可只起 HTTP 不开定时任务 |

### 3.4 `config/templates/` — 出站文案

六份 Jinja2 模板，`StrictUndefined`：**模板里缺变量会直接报错，不会发出半截邮件**。

| 模板 | 用途 |
|---|---|
| `feishu_dispatch.md` | 给主管的简历包摘要（份数、通过名单、zip 名） |
| `resume_package_summary.md` | zip 内的 `00-筛选说明.md` |
| `feishu_receipt.md` | 「已通知 N 人」回执 |
| `interview_notice.md` | 面试通知邮件 |
| `interview_update.md` | 改期确认邮件 |
| `feishu_candidate_reply.md` | 候选人回信（改期/放弃/确认）的飞书汇报 |

---

## 4. 开发纪律

**分层（单向依赖，不许回头）**

| 层 | 允许 | 禁止 |
|---|---|---|
| `api/` | 解析参数、注入依赖、返回 schema | 写业务判断、拼 SQL |
| `service/` | 编排用例，调 repository / client | 直接写 SQL、直接拼 HTTP 请求 |
| `repository/` | 单聚合根的数据访问 | 调外部 API、跨聚合编排事务 |
| `client/` | 封装外部系统并归一化返回 | 碰数据库、写业务规则 |
| `model/` | 表结构与枚举 | 任何业务逻辑 |

请求路径：`middlewares → api/v1 → core/deps（装配请求级 Services）→ service → repository → model`。

**四条硬纪律**

1. **异常隔离**：单步失败不阻断整轮。一份简历解析失败记告警后跳过；一个部门分发失败不影响别的部门。
2. **测试先行**：核心逻辑必须有单测，外部服务全部 mock；`pytest` 必须离线可跑。
3. **幂等防重**：重复投递只留 1 条（`is_duplicate` / `duplicate_of_id`，只追加事件）；重复回执按幂等忽略；同轮已发通知不重发。
4. **时间统一存 naive UTC**，展示时经 `core/timeparse.py` 转 `Asia/Shanghai`。混存带时区/不带时区会在比较时炸。

**降级顺序（三个 LLM 节点共用）**：规则先跑 → 主模型（45s 超时）→ `LLM_FALLBACK_MODELS` 逐个换 → 规则兜底 → 都不行挂牌人工。
**只在调用失败时降级，不因「结果不理想」降级**——否则等于让规则偷偷覆盖模型，线上出问题查不出来。

---

## 5. 机器人的五条判定规则

决定它「什么时候说话、什么时候闭嘴」：

1. **时间只来自主管。** 简历里的期望时间、候选人提议的时间都不是结论。
2. **缺信息只追问。** 缺姓名或缺时间就回一条消息问，绝不猜一个时间发出去。
3. **姓名必须接地。** 只在「本轮发给这位主管的名单」里匹配，对不上就请人确认，不猜。
4. **改期必须主管点头。** 候选人说改就改，等于把主管的日程当空气。
5. **重复投递不覆盖。** 只追加事件，首次记录才代表这个人的真实状态。

---

## 6. 状态机（双渠道共用）

```
received                                    已收取
   │ 解析成功
   ▼
parsed                                      已解析
   ├─ 硬性条件不过 ──→ screened_rejected     筛选淘汰（终点，不进任何后续）
   ▼
screened_passed                             筛选通过
   │ 打包发主管（飞书 + 邮件抄送）
   ▼
dispatched                                  已发主管
   │ 主管给出名单 + 面试时间
   ▼
supervisor_feedback                         主管已反馈
   │ 面试通知邮件发出（★ 本轮终点）
   ▼
interview_notified                          已邮件通知
   ├─ 候选人申请改期 ──→ reschedule_pending ──(主管点头 + 补发确认)──→ 回到 interview_notified
   ├─ 候选人放弃     ──→ declined            （终点）
   └─ 候选人确认到场 ──→ completed           （终点）
```

| 约束 | 说明 |
|---|---|
| 枚举冻结 | 10 个，不可增删改（`model/candidate.py::CandidateStatus`） |
| 先到先处理 | 主管反馈无论来自飞书还是邮件，先到的落地，另一渠道幂等忽略 |
| 淘汰者止步 | `screened_rejected` 不进入分发、通知任何环节 |
| 未归位 | 停在 `received` 并飞书告警等人工分配 |

三张留痕表：`candidates`（简历、得分、状态）、`messages`（收发全文、解析结果、回执）、`events`（每次状态变化、操作者、前后状态）。

---

## 7. 已知限制与未决项

| 项 | 现状 |
|---|---|
| 无凭证时只能 mock | 出站内容全量落库落盘可审阅，但不会真的发出去 |
| 飞书加密推送 | `live` 模式下若 SDK 不提供 `AESCipher` 会抛 `configuration_error`，需在后台关加密或网关层解密；**不自行实现 AES** |
| 归位靠关键词 | 简历没写岗位且关键词不足时不会瞎猜，停在 `received` 等人工 |
| 单进程调度 | APScheduler 内存 job store；多副本部署需换共享 job store + 分布式锁 |
| 建表用 `create_all` | 未引入 Alembic（表结构稳定后再上） |
| 越界日期 | 「下个月 30 号」这类在 28/29 天月份里解析为 `None`，退化为「只给时间」分支 |
| Docker 链路 | `Dockerfile` / `docker-compose.yml` 已写，需本机 Docker 起来实跑一次验收 |

---

## 8. 文档地图

| 文件 | 内容 |
|---|---|
| `README.md` | 流程图、目录结构、本地怎么跑 |
| `API.md` | 全部 HTTP 接口与错误码 |
| `AGENT.md` | 本文件：边界、环境、配置、纪律、状态机 |
| `DECISIONS.md` | 决策台账：为什么这么设计、与契约的偏差、明确不做 |
| `docs/改动记录.md` | **每次改动都追加一条**：改了什么 / 为什么 / 改了哪些文件 / 怎么验证 |
| `最简测试指南.md` | 用真邮箱 + 真飞书实测一遍的操作手册 |
| `docs/unattended-protocol.md` | 无人值守部署、监控、故障处置 |
| `docs/database-setup.md` | PostgreSQL 配置与旧库迁移 |
| `docs/llm-setup.md` | LLM 接入与降级配置 |
| `docs/known-limitations.md` | 限制的修复记录 |
| `docs/architecture.md` | 分阶段时序图 |
| `docs/testing-report.md` | 测试覆盖矩阵 |
