# DECISIONS — 设计决策与偏差台账

> 这份文件只记**代码里看不出来的东西**：为什么这样设计、哪些做法被明确排除、与契约有哪些偏差。
> 判定优先级：产品文档 > 开发契约 > 本文件记录的默认决策。
>
> 维护规则：决策一旦落地就不要再改文字（改了等于抹掉当时为什么这么选）；
> 新决策追加编号，被推翻的决策标注「已被 D-0xx 取代」而不是删除。

---

## 1. 设计决策

### D-001 飞书/邮件/LLM 三态模式（mock / live / off）
外部系统均以"可观测、可替换"的方式接入：`FEISHU_MODE`、`MAIL_MODE`、`LLM_MODE`。mock 不是假数据分支，而是**只把出站写到 DB/磁盘**，入站解析仍是真实报文格式。这样评审可以在没有任何凭证的情况下看到完整闭环与留痕，也避免把"跑不起来"归咎于环境。

### D-002 时间统一存 naive UTC
SQLite 不保留时区，混存 aware/naive 会在比较时炸。约定：所有 `DateTime` 列存 **naive UTC**，展示统一经 `core/timeparse.py` 转 `Asia/Shanghai`。代价是读库时需要显式转换，收益是跨 SQLite/PostgreSQL 行为一致。

### D-003 单一邮件轮询入口，避免"两套 poller 互吞邮件"
契约里 `service/ingestion.py` 与 `service/inbound_email.py` 都涉及收信。若各自 `poll_inbox`，先跑的那个会把对方的邮件消费掉（mock 模式靠 watermark，IMAP 靠 UID）。因此**运行时只有一个轮询器**：`InboundEmailService.poll_once()` 拉一次，再按"线程命中 → 主管；线程命中 → 候选人；带简历附件 → ingestion；其余忽略"分流。`IngestionService.ingest_from_mailbox` 仍保留，供脚本单独使用。每日任务通过 `poll_once(cycle_id=...)` 收简历，高频任务通过 `poll_once()` 只处理回复——**语义一致、不会丢信**。

### D-004 主管除飞书外，另发一份简历包邮件
产品文档写的是"飞书发压缩包"，但验收截图明确要求主管"直接在原邮件上回复"。若主管邮箱里没有那封邮件，这条路径物理上不成立。因此：飞书发文字摘要 + 压缩包文件消息；同时把同一压缩包发到主管邮箱（`Reply-To: reply+<token>@domain`）并登记 `EmailThread(kind=resume_package)`。这是对两条验收路径的**同时满足**，不是越界功能。

### D-005 姓名解析采用"接地匹配"而非自由识别
主管回复里的姓名一定来自本轮已发名单。`match_roster_names()` 只在名单内匹配，避免把"下周""技术部""12 份"当人名；LLM 路径同样做名单过滤（`test_llm_names_are_grounded_to_roster`）。名字对不上名单时**不猜**，回一条飞书消息请主管确认。

### D-006 缺信息只追问，不发错邮件
`_handle_schedule` 的三条分支：无姓名 → 回格式提示；有姓名无时间 → 追时间；有时间无姓名 → 追名单。任何一条都不触发邮件，符合"只有明确结论才发通知"。

### D-007 改期必须主管确认
候选人回信说"改到周四下午 3 点"时，机器人**先**在飞书问主管，主管回"同意"或给出新时间后才补发确认邮件（`test_candidate_reschedule_and_supervisor_approval`）。理由：面试时间的所有权在主管，机器人不得替主管答应。

### D-008 新增 `core/timeparse.py`
属于 `core/`（横切关注点）内的新模块，未改变契约的目录结构。理由：中文相对时间（下周X / 下午X点 / 十一）是跨层复用的纯逻辑，放在任何 service 里都会造成重复。

### D-009 `email_threads` 增加 `message_id_index` 扁平列
`message_ids` 仍保留 JSON（审计用），另加一列空格拼接字符串用于 `LIKE` 匹配：SQLite 对 JSON 列的包含查询支持差，扁平列在 SQLite/PostgreSQL 上行为一致。属于同一聚合根内的字段补充，未新增表。

### D-010 建表用 `create_all`，暂不引入 Alembic
MVP 阶段表结构仍在动；引入迁移框架会增加评审噪音。已在 `core/db.py::init_models` 注明生产可替换为 Alembic。

### D-011 `unstructured` 为"首选但可缺省"的解析器
`client/resume_parser.py` 先尝试 `unstructured.partition`，缺失或失败时降级 `pypdf` / `python-docx`，行为对上层一致，并在返回的 `warnings` 里留痕。理由：其依赖体积大、原生库不稳（见 D-018 与 `docs/known-limitations.md`），不能成为闭环前置条件。

### D-012 依赖安装走 PyPI 官方源
本机访问镜像源时拿不到大体积索引页，改用 `pypi.org` + `--timeout 300`。仅影响开发环境，不影响生产镜像（Docker 内默认 PyPI）。

### D-013 旧 demo 文件改为分阶段提交
`D:\!pythonproject\hr_ai_robot` 原本是往期学员 demo（`Demo1.py`、`feishu_bot.py` 等）。正式工程结构建成后，历史已被清空并重建为新的提交线；旧 demo 不再保留在 `develop` 上。**结论：这条历史只影响还留着旧 clone 的人，仓库内无需再做归档。**

### D-014 事件解密的依赖边界
`lark_oapi` 负责 token/加解密。`client/feishu.py::_decrypt` 调用 SDK 的 `AESCipher`，若当前 SDK 版本不提供该工具，会抛 `ConfigurationError` 并提示——此时应在飞书后台关闭"加密推送"，或在网关层解密。**不自行实现 AES**（契约明确要求不重复造轮子）。

### D-015 职位规则以 YAML 为唯一真源
每轮任务开始 `PositionRepository.sync_from_config()` 覆盖数据库快照；配置里删掉的职位置 `active=false` 而不物理删除（保留历史简历关联）。HR 改 YAML → 下一轮生效，无需重启。

### D-016 邮件署名不再重复联系人
`_signature()` 原先把 `company.hr_contact` 再印一遍，而模板正文已有一行「联系人：…」，候选人会看到两份同样的联系方式。改为署名只留「公司名｜HR 招聘组」+ 机器人自动发送提示；联系人信息统一由正文 `- **联系人**：` 一行承载。同时把 `interview_notice.md` 的 `{{ notes }}` 包进 `{% if notes %}`，避免空备注留下两行空白。

### D-017 提供 `scripts/demo.py` 作为验收入口
真实飞书/邮箱/LLM 凭证可能都不可用，验收方需要"看得见的证据"。`scripts/demo.py` 用纯 mock 走完 投递→筛选→打包分发→主管飞书回复→面试通知→候选人改期→主管同意→补发确认，并把每一步机器人真正发出的飞书文本/邮件正文打印出来；`--keep` 用于在既有数据上复跑。该脚本不新增任何业务逻辑，仅编排既有 service。

### D-018 `cycle_id` / `object_id` 用 `BigInteger` 而非 `Integer`
`cycle_id` 由 `int(now.strftime("%Y%m%d%H%M"))` 生成 12 位整数（如 `202611041734` ≈ 2026 亿），超出 PostgreSQL `INTEGER` 的 int32 上限（21.47 亿）会报 `value out of int32 range`。SQLite 的 INTEGER 是 64 位，所以这个 bug 在 SQLite 上一路潜伏，切 PG 才暴露。
处理：只把列类型 `Integer` → `BigInteger`，字段含义与生成逻辑不动。不改生成逻辑的理由：改成小整数会连带改打包目录名、日志、测试多处，还丢掉时间可读语义；位宽扩展是最小改动。

---

## 2. 与契约文字的偏差（如实记录）

| 契约原文 | 实际情况 | 处理 |
|---|---|---|
| `Mr-KID-github/Dify-FeiShu-bot`（**FastAPI** 路由范式） | 该仓库 `app.py` 是 **Flask**（`from flask import Flask`，`@app.route('/webhook')` + 线程池），并非 FastAPI | 仅借鉴"单 bot 单 `/webhook` + 挑战应答 + 事件分发"范式，用 FastAPI `APIRouter` 实现 |
| `srbhr/Resume-Matcher`（官方口径 28k+，许可证以仓库为准） | 运行时读取：29,080 star，`Apache-2.0`（非 MIT） | 台账写实际值 |
| `Anil-matcha/open-agent-email`（约 2.1k star） | 运行时 2,121 star，Apache-2.0 | 台账写实际值 |
| `emersion/python-emailthreads`（44 star，`groupemailthread.py` 式实现） | 运行时 49 star，已归档；实际文件为 `emailthreads/threads.py` | 标注 `LOW_STAR_EXCEPTION` 与真实路径 |
| `sabuhish/fastapi-mail`（1k star） | 运行时 1,016 star，MIT | 台账写实际值 |
| `getsentry/sentry` 的 `GroupEmailThread` | `src/sentry/models/groupemailthread.py` 存在（HTTP 200 核验）；但 GitHub API 返回 `NOASSERTION`，仓库为 Functional Source License（非 OSI 开源） | 只借鉴模型设计，**不引用任何代码** |

> 借用清单的完整版与许可证隔离说明见 `docs/innovation-statement.md`。

---

## 3. 明确不做（防止后来者"顺手补全"）

这些不是遗漏，是产品边界或护栏要求。想加之前先回来读这一节。

| 看起来该做 | 为什么不做 |
|---|---|
| Web / 小程序 / APP 任何界面 | 产品定位是机器人，界面即飞书对话与邮件；`tests/test_api_misc.py` 强制校验仓库无前端文件 |
| 面试官角色、一面/二面、面试评价、笔试测评 | 没有面试官这个概念，不做轮次管理 |
| 管理员、人才库共享、候选人自助改期页面 | 范围外；候选人只收发邮件 |
| 被淘汰候选人的感谢信 | 只记 `screened_rejected` 状态与事件 |
| 固定次数 / 立即 / 退避重试 | 会给候选人连发多封面试邮件。失败走"下一轮按幂等键补发"，不引入重试策略 |
| 自动补发改期确认邮件 | 改期必须主管确认（D-007），自动补发有越权风险，留人工 |
| 补发飞书简历包**文件**消息 | 文件消息不能走文本通道补发，需重新上传，留人工 |
| 为"未归位"新增状态枚举 | 状态机 10 个枚举冻结，不可增删改；未归位者停在 `received` |
| 引入 Alembic / 迁移框架 | 见 D-010 |
| 给 LLM 加缓存 / 重试队列 / 新表 | 超出本轮边界；LLM 只换运输层，不换抽取契约 |
| 顶层 `import litellm` | 规则路径（`LLM_MODE=off`）不应强制加载重依赖，故延迟 import |
| 删除 `core/db.py` 的 sqlite 分支 | 2026-11-14 已删除，`aiosqlite` 依赖一并移除，全链路 PG-only |
| 把「LLM 返回了但抽取不理想」当降级理由 | 会绕过 D-005 姓名接地，是隐性事故。降级只发生在调用失败 / 超时 / 无 key |

---

## 4. 未决项

| 项 | 现状 | 需要做什么 |
|---|---|---|
| Docker 链路未实跑 | `Dockerfile` 与 `docker-compose.yml` 只做过语法与结构审阅，本机 Docker daemon 一直没起来，这条链路一次都没跑通 | 启动 Docker Desktop 后 `docker compose up --build`，确认 `http://localhost:8000/health` 返回 ok；再对照 `README §11` 与 `docs/unattended-protocol.md` 的部署注意事项 |
| 「下个月 30 号」这类越界日期 | 只有 28/29 天的月份里 `_parse_date` 返回 `None`，流程退化为"只给时间"分支（与「2月30号」历史行为一致） | 要彻底修得改 `parse_chinese_datetime` 主流程，属重构，未做 |
| 单进程调度 | APScheduler 内存 job store | 多副本部署前换共享 job store + 分布式锁，见 `docs/unattended-protocol.md` |
