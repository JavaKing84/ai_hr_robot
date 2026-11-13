# 03 · 业务流程与调用链

每条链标注**真实文件:行号 + 函数**。凡源码中不存在的能力，明确写"未发现实现"，不补画。

---

## 1. 定时任务触发

```mermaid
sequenceDiagram
    autonumber
    participant U as 进程启动
    participant L as factory/lifespan.py
    participant D as core/db.py
    participant S as APScheduler
    participant O as Orchestrator

    U->>L: lifespan(app) 进入  (factory/lifespan.py:85)
    L->>D: init_models(engine) → Base.metadata.create_all  (core/db.py:35-44)
    alt SCHEDULER_ENABLED=true
        L->>S: build_scheduler(container)  (factory/lifespan.py:48)
        S->>S: add_job(run_daily_job, CronTrigger.from_crontab(cron), max_instances=1, coalesce=True)
        opt INBOUND_POLL_SECONDS > 0
            S->>S: add_job(poll_inbound_job, IntervalTrigger(seconds=N))
        end
        L->>S: scheduler.start()
    else false
        L->>L: 只起 HTTP，日志 scheduler=disabled  (:99-100)
    end
    S->>L: 到点 → run_daily_job(container)  (:28-35)
    L->>O: 新 session → build_services → run_daily_cycle()  (service/orchestrator.py:73)
```

关键事实：
- cron 取值优先级 `SCHEDULE_CRON` → `positions.yaml:schedule.cron` → `"0 9 * * *"`（`factory/lifespan.py:52`）。
- job 内部异常被 `try/except` + `rollback` + `logger.exception` 吞掉，调度器不会因此丢任务（`factory/lifespan.py:33-35`）。
- **未发现**：跨进程去重、任务持久化、补跑游标。`misfire_grace_time=3600` 只保证错过一小时内仍执行。

---

## 2. 邮箱收取简历（IMAP 增量 / mock 目录）

```mermaid
flowchart TD
    A["InboundEmailService.poll_once(cycle_id)  service/inbound_email.py:87"] --> B["MailboxClient.poll_inbox()  client/mailbox.py:214"]
    B -->|MAIL_MODE=off| Z["返回空列表"]
    B -->|mock| C["_mock_poll() 读 var/mock_inbox/*.eml<br/>按 .state.json 的 mtime 去重  :222-244"]
    B -->|live| D["_live_poll()  imaplib  :263-306"]
    D --> D1["login → _send_imap_id（网易系必需）→ select(INBOX)"]
    D1 --> D2["uid search 'UID last_uid+1:*'  → 只取 > last_uid 且限 imap_poll_limit 条"]
    D2 --> D3["uid fetch RFC822 → parse_email_bytes()"]
    D3 --> D4["写回 var/imap_state.json 的 last_uid 水位  :303-304"]
    C --> E["逐封 handle_email()  :109"]
    D4 --> E
```

事实：
- UID 水位是"只进不退"：`last_uid = max(last_uid, uid)`（`client/mailbox.py:297`），因此**同一封邮件不会被处理两次**，但也意味着"处理中崩溃"的邮件不会自动重投（无重试队列）。
- `imap_poll_limit=30` 是**单轮上限**（`client/mailbox.py:290`），如果积压超过 30 封，靠下一轮继续推进水位。
- 网易系邮箱在 `SELECT` 前强制 `ID` 命令，否则报 `Unsafe Login`（`client/mailbox.py:246-261`）——这是实测问题的修复，非通用协议要求。

---

## 3. 附件保存与文档解析（含子进程隔离）

```mermaid
sequenceDiagram
    autonumber
    participant I as IngestionService
    participant P as ResumeParser
    participant SP as core/safe_process.py
    participant U as unstructured / pypdf / docx

    I->>P: save_and_parse(data, filename)  (client/resume_parser.py:513)
    Note over I: 实际经 asyncio.to_thread(run_in_subprocess, ...) 调用<br/>service/ingestion.py:339-345
    I->>SP: run_in_subprocess(parser.save_and_parse, data, filename, timeout=60)
    SP->>SP: multiprocessing spawn 子进程  (core/safe_process.py:112-119)
    SP->>P: 子进程内执行
    P->>U: _extract_text(path)  (client/resume_parser.py:548)
    alt unstructured 可用
        U-->>P: 文本 + engine=unstructured
    else 失败/未安装
        P->>U: _pdf_text / _docx_text 降级  (:669,684)
        U-->>P: 文本 + warnings
    end
    P-->>SP: ParsedResume(text, path, engine, warnings)
    SP-->>I: SubprocessResult(ok=True)
    alt 子进程段错误 / 超时 / 非 0 退出
        SP-->>I: ok=False + reason
        I->>I: 降级为空文本 ParsedResume(engine="subprocess-failed")<br/>service/ingestion.py:346-362（不抛异常）
    end
```

事实：
- 简历原件落盘路径由 `ResumeParser.save_and_parse` 写入 `settings.resumes_dir`（`var/resumes/`），文件名带时间戳+短哈希（见 `var/e2e/var/resumes/20261113150539-ed52d5-陈昊然-简历.txt` 这类实际产物）。
- 解析上限 60 秒（`service/ingestion.py:45` `_PARSE_TIMEOUT_SECONDS`）。
- `core/safe_process.py` 的核心设计：**先消费结果队列再 join**，避免 feeder 线程与主进程互等被误判超时（该模块 docstring 记录了一次 8.7MB PDF 被误杀的实测）。
- `ParsedResume.text` 入库时截断 50,000 字符（`service/ingestion.py:264`）。

---

## 4. LLM 结构化抽取（规则先跑，模型补全）

```mermaid
flowchart TD
    A["extract_candidate(text, filename, positions)  service/extraction.py:186"] --> R["parser.prefill(text, filename) 规则抽取<br/>client/resume_parser.py:593"]
    R --> Q{"llm.enabled?"}
    Q -->|否| OUT1["直接返回规则结果（method=rule）"]
    Q -->|是| L["llm.complete_json(RESUME_SYSTEM, build_resume_prompt)"]
    L -->|异常/JSON 不合法/schema 校验失败| OUT1
    L --> M["_merge_resume(rule, llm)  :121-169"]
    M --> OUT2["按字段合并：姓名/邮箱/电话/学历/院校/专业/年限 → 规则优先<br/>技能合并去重；项目亮点/摘要/岗位意向 → 模型优先"]
```

事实与理由（docstring `service/extraction.py:121-132` 直接写明）：
- 基础字段规则优先，因为"正则与词典校验比模型可靠"；年限用"工作时间区间并集"，比模型读文字准。
- 简历里没有的字段（如 BOSS PDF 的字符分隔排版）由 `_guess_name` 从**主题/附件名**兜底（`service/ingestion.py:562-579`），这是 2026-11-12 的实测修复。
- 三个 LLM 节点共用同一降级策略：**只在调用失败时降级，不因结果不理想降级**（`AGENT.md:145-146` 明写；`client/llm.py:341-355` 只在异常时换供应商）。

---

## 5. 去重与职位归类

```mermaid
flowchart TD
    A["ingest_resume_bytes()  service/ingestion.py:173"] --> B["list_active() 取在招职位"]
    B -->|为空| U1["_store_unmatched('没有在招职位')  :199-203"]
    A --> C["_resolve_position()  :486-546"]
    C --> C1["① 显式 position_code / 职位名"]
    C1 --> C2["② 主题+附件名里精确包含职位标题（长标题优先）"]
    C2 --> C3["③ 简历 position_hint 与标题互含"]
    C3 --> C4["④ 关键词投票（正文前 4000 字 + 意向 + 主题 + 文件名）"]
    C4 -->|全不中且职位>1| U2["_store_unmatched('未写明岗位且关键词不足')"]
    C4 -->|命中| D["find_duplicate(email, phone)  repository/candidate.py:17"]
    C -->|仅 1 个职位| D
    D -->|命中首次记录| DUP["只刷新 last_seen_at + 记 candidate.duplicate 事件<br/>直接返回首次记录（不新建行）  :220-245"]
    D -->|未命中| N["新建 Candidate(status=received) → 事件 candidate.received → status=parsed → 事件 candidate.parsed"]
    N --> S["ScreeningService.screen_candidate()  → 见链 6"]
```

事实：
- 归位顺序的动机写在 docstring：BOSS 的主题/附件名里明写岗位，"岗位名比关键词强得多"（`service/ingestion.py:496-501`），修的是"7 份 Python 简历被算法工程师抢走"的实测故障。
- **未归位不是失败**：`_store_unmatched` 新建 `Candidate(position_id=None, status=received)`，记 `candidate.unmatched_position` 事件，并向"第一个有效主管 open_id"发飞书告警（`service/ingestion.py:364-452`；接收人由 `core/deps.py:196-206` 派生）。
- 去重口径：`email` **或** `phone` 任一命中即重复（`repository/candidate.py:17-32`），且只在 `is_duplicate=false` 的记录里找（该列从不被置 true，等价于全表）。
- 平台共享发件人（`cv@notice.zhipin.com` 等）不会写进 `candidates.email`（`service/ingestion.py:454-479` + `service/email_threading.py:51-59`）。

---

## 6. 规则筛选 + AI 辅助分析

```mermaid
flowchart TD
    A["screen_candidate(candidate, position)  service/screening.py:448"] --> B["ScreeningConfig.model_validate(position.screening)"]
    B --> C["evaluate() 纯函数  :136 —— 先算硬条件+规则分"]
    C --> C1["白名单 always_pass_names 命中 → 直接 100 分通过  :151-161"]
    C --> C2["硬条件逐条 _check_operator  :87-133<br/>major/school 抽空 → unknown，跳过淘汰（记 hard_unknown）"]
    C --> C3["加权项 Σ(命中比例×权重)/Σ权重×100  :204-229<br/>major/school 抽空 → 不进分母"]
    C2 --> D{"硬条件有失败?"}
    D -->|有| E["跳过语义分，note='硬性条件未通过'"]
    D -->|无| F["_semantic_score()  :358<br/>一次调用同时拿语义分 + 深度解析（省 token）"]
    F --> G["evaluate(..., semantic_score, semantic_applied=not semantic_shadow)"]
    E --> G
    G --> H["写 score / score_detail / hard_failed / status<br/>screened_passed | screened_rejected + 事件  :476-505"]
```

事实：
- 打分公式与 `prompt/screening.md` 一致：`Σ(命中比例 × 权重) / Σ权重 × 100`。
- **影子模式**（`SEMANTIC_SHADOW=true`，本机生效）：语义分照常调用与落库，但 `semantic_applied=False`，通过与否仍按规则分（`config/settings.py:75-80`、`service/screening.py:463-465`）。
- 语义分**不能推翻硬条件**：硬条件失败时根本不调用模型（`service/screening.py:459-461`）。
- 深度解析只做展示：`_normalize_deep` 把维度分夹到 0–100、列表裁到 3 条，全 0 视为"照抄示例"而降级（`service/screening.py:293-327,436-444`）。
- 淘汰是终态：`screened_rejected` 之后不会进入分发/通知（`AGENT.md:190`；代码侧由 `dispatch_cycle` 只取 `screened_passed` 保证）。

---

## 7. 简历包生成与主管推送（飞书 + 邮件副本）

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant N as NotificationService
    participant FS as FeishuClient
    participant M as MailboxClient
    participant DB as PostgreSQL

    O->>N: dispatch_cycle(cycle_id)  (service/notification.py:87)
    N->>DB: list_by_cycle → 过滤 status=screened_passed → 按 position_id 分组
    loop 每个有通过者的职位
        N->>N: _build_package()  zip + 00-筛选说明.md  (:209-265)
        N->>FS: push_text(摘要)  → 群 or open_id  (:71)
        N->>FS: push_file(zip)   → 群需 FEISHU_CHAT_ID  (:77,157)
        N->>M: _send_supervisor_email() 附件 zip + Reply-To reply+token  (:267-333)
        N->>DB: 每候选人 status=dispatched + candidate.dispatched 事件  (:175-192)
    end
    Note over N: 单职位 try/except：失败只写进 PackageDispatchResult.error  (:104-114)
```

事实与风险：
- 简历包命名为 `{部门}-{职位}-简历{N}份-{yyyymmddHHMM}.zip`，落在 `var/packages/<cycle_id>/`（`service/notification.py:217-221`）。
- 主管邮件是本链的**关键一环**：没有这封邮件，主管就没法"在原邮件上回复"（`AGENT.md:74`）；未配 `supervisor_email` 时直接跳过并返回 `ok=False, detail="主管未配置邮箱"`（`service/notification.py:276-277`）。
- 群推送细节：有 `FEISHU_CHAT_ID` 用**应用身份**发（`im/v1/messages`），只有 webhook 时才用自定义机器人；文件必须走应用身份上传（`client/feishu.py:85-243`），失败会在群里补一句人话提示（`client/feishu.py:245-258`）。
- ⚠️ **状态推进不看发送结果**：`candidate.status = DISPATCHED` 在 `_dispatch_position` 里无条件执行（`:177`），`feishu_text.ok`/`file_result.ok` 只影响返回值与 `messages.status`。

---

## 8. 主管通过飞书反馈

```mermaid
sequenceDiagram
    autonumber
    participant F as 飞书开放平台
    participant API as api/v1/feishu.py
    participant C as FeishuClient
    participant FB as FeedbackService
    participant AG as AgentService

    F->>API: POST /api/v1/feishu/webhook
    API->>C: decode_webhook(body, headers)  (client/feishu.py:402)<br/>encrypt → AESCipher；token 比对
    alt challenge
        API-->>F: {"challenge": ...}
    end
    API->>C: event_type + normalize_event  (:430-446)
    C-->>API: InboundMessage(channel=feishu, sender_id=open_id, text, kind)
    API->>FB: handle(inbound)  (service/feedback.py:112)
    FB->>FB: _resolve_supervisor → supervisors 表按 open_id 找  (:728)
    alt 找不到且 sender 是 HR_FEISHU_OPEN_ID
        FB->>FB: _any_supervisor() 借用任一有职位的主管身份  (:748-754)
    end
    FB->>FB: _record_inbound（messages 表留痕）
    FB->>AG: plan(...) 意图路由（LLM 可用时）  (service/agent.py:93)
    FB-->>F: FeishuWebhookResponse(ok, event_type, handled, detail)
    Note over API: 注意：回给主管的实际消息是 FB 内部调 notify_supervisor 主动推送，<br/>不是 HTTP 响应体
```

事实：
- 只支持两种事件：`im.message.receive_v1`、`card.action.trigger`（`client/feishu.py:442-446`），其它事件返回 `handled=false`。
- 验签现状：**只比对 verification token**，未见 `X-Lark-Signature` 校验（`client/feishu.py:412-415`）。
- 加密体依赖 `lark_oapi.core.utils.AESCipher`，取不到即抛 `ConfigurationError`（`client/feishu.py:418-428`），`AGENT.md:202` 明确"不自行实现 AES"。
- 回消息走主动推送而非回调响应（`service/feedback.py:257,283,352`），因此**飞书侧需要保证应用有发消息权限**，与"事件回调"是两套权限。

---

## 9. 主管通过邮件线程反馈

```mermaid
sequenceDiagram
    autonumber
    participant M as MailboxClient
    participant IB as InboundEmailService
    participant T as EmailThreadService
    participant FB as FeedbackService

    M-->>IB: poll_inbox() 返回 InboundEmail（含 in_reply_to/references/to_addresses）
    IB->>T: match(email)  (service/email_threading.py:92)
    T->>T: ① find_by_any_message_id([in_reply_to]+references)  (:180-184)
    T->>T: ② To/Cc 里的 reply+token 别名  (:186-191)
    T->>T: ③ 发件人=主管邮箱 → 其最近职位  (:194-200,142-158)
    T->>T: ④ 发件人=候选人邮箱 → 其面试通知线程  (:194-200,160-175)
    T-->>IB: ThreadMatch(role=supervisor, thread.kind=resume_package)
    IB->>FB: handle(InboundMessage(channel=email, ...))  (service/inbound_email.py:118-140)
    FB-->>IB: FeedbackOutcome(handled, action, detail)
    IB->>T: attach_thread()（没命中线程时补登记，保证下次能挂上）  (:202-223)
```

事实：
- **带简历附件的邮件一律优先入库**，即使线程匹配命中了主管/候选人（`service/inbound_email.py:113-118`、`service/email_threading.py:62-73,97,130-135`）。修复的是"平台共用 noreply 发件人把 9/20 封简历吞成回信"的实测事故。
- 平台通知地址（`noreply`/`notice`/`cv@` 等）不作为身份依据（`service/email_threading.py:45-59,139-140`）。
- 线程表里同时存 `message_ids`（JSON，审计）与 `message_id_index`（空格拼接字符串，LIKE 匹配走索引）（`model/email_thread.py:36-40`）。
- 回复地址 `reply+<token>@<HR邮箱域名>`（`client/mailbox.py:91-93`），token 兜底匹配在 `service/email_threading.py:226-231`。

---

## 10. 反馈解析、校验与状态更新

`FeedbackService.handle()` 的分支顺序（`service/feedback.py:112-208`，顺序本身是设计）：

```mermaid
flowchart TD
    A["handle(inbound)"] --> B{"能认出主管?"}
    B -->|否| B1["记 feedback.supervisor_unknown，返回 handled=false"]
    B -->|是| C{"名下有在招职位?"}
    C -->|否| C1["返回 no_position"]
    C -->|是| D["_record_inbound 留痕"]
    D --> E["场景0：_handle_interview_approval  (:211-299)<br/>有待确认草稿 + 无时间词 + 命中确认/取消词"]
    E -->|命中| E1["confirm/cancel_pending_interview_notices → 飞书回执"]
    E -->|未命中| F["AgentService.plan() 意图路由"]
    F -->|is_read 或 UNKNOWN| F1["agent.answer()：检索事实 → 组句 → 推送（不写库）"]
    F -->|REJECT| F2["_handle_reject()：只改状态+事件，绝不给候选人发信  (:487-570)"]
    F -->|RESCHEDULE 且有 pending| G["_handle_reschedule_decision()  (:610-684)"]
    F -->|SCHEDULE/RESCHEDULE/无 plan| H["_handle_schedule()  (:306-484)"]
    H --> H0{"名单里有人吗?"}
    H0 -->|没有| H1["_format_hint 追问格式（不猜）"]
    H0 -->|有| H2{"有面试时间吗?"}
    H2 -->|没有| H3["追问时间（不猜、不发）"]
    H2 -->|有| H4["逐人 status=supervisor_feedback + 事件<br/>→ send_interview_notices() → 见链 11"]
```

关键校验（"机器人的五条判定规则"在代码里的落点）：

| 规则（`AGENT.md:150-159`） | 代码落点 |
|---|---|
| 时间只来自主管 | `_handle_schedule` 只接受 `schedule.interview_time`，候选人提议的时间只用于"请示主管"（`service/feedback.py:639`） |
| 缺信息只追问 | 缺名 `:349-366`；缺时间 `:368-384` |
| 姓名必须接地 | `names = [n for n in schedule.candidate_names if n in set(roster_names)]`（`:349`）；Agent 写操作再过滤一次（`service/agent.py:161-163`） |
| 改期必须主管点头 | 候选人回信只置 `reschedule_pending`（`service/inbound_email.py:223-225`），补发确认仅在 `_handle_reschedule_decision` 里 |
| 重复投递不覆盖 | `service/ingestion.py:220-245` |

---

## 11. 面试通知邮件发送（含人工确认闸门）

```mermaid
flowchart TD
    A["send_interview_notice(candidate, position, interview_time, confirmed=False)  service/notification.py:336"] --> B{"候选人邮箱为空?"}
    B -->|是| B1["返回 ok=False（不发）"]
    B -->|否| C["渲染 interview_notice.md + 生成 subject"]
    C --> D["找到/新建 EmailThread(INTERVIEW_NOTICE)<br/>message_id 可复用（补发幂等）"]
    D --> E{"messages.find_delivered(message_id)?"}
    E -->|已发过| E1["幂等跳过，返回 ok=True"]
    E -->|未发| F{"INTERVIEW_NOTICE_REQUIRE_CONFIRM 且 not confirmed?"}
    F -->|是| G["_park_interview_notice()：status=interview_pending_confirm<br/>写 messages.status=pending_approval，绝不投递  (:464-525)"]
    F -->|否| H["mailbox.send(...) → 写 messages + 事件<br/>status=interview_notified  (:399-461)"]
    G --> I["request_interview_approval()：飞书请示 HR  (:585-667)"]
    I --> J["HR 回「确认 / 确认 1,3 / 取消」→ feedback._handle_interview_approval"]
    J --> K["confirm_pending_interview_notices() → 以原 Message-ID 重发  (:540-583)"]
```

事实：
- 闸门默认开启（`config/settings.py:69` 默认 `True`；本机 `.env` 生效为 true）。
- 确认后沿用**原 Message-ID**（`service/notification.py:575`），因此重复确认不会重复发信。
- 取消会把候选人退回 `dispatched` 并清空 `interview_time`（`:686-689`）。
- 本机 `INBOUND_POLL_SECONDS=0`，但人工确认走飞书 webhook，不依赖轮询。CLI 入口：`scripts/interview_notices.py`；`.bat`：`确认发送面试通知.bat`、`查看待确认面试通知.bat`。

---

## 12. 候选人确认 / 改期 / 放弃

```mermaid
sequenceDiagram
    autonumber
    participant M as 邮箱
    participant IB as InboundEmailService
    participant EX as ExtractionService
    participant DB as candidates
    participant FS as 飞书（主管）

    M-->>IB: 候选人回信（线程命中 interview_notice）
    IB->>EX: extract_candidate_reply(body, candidate_name, position_title, current_time)  (service/extraction.py:283)
    EX->>EX: detect_intent（decline→reschedule→confirm 关键字顺序）  (:103)
    EX->>EX: parse_reschedule_time() 抽提议时间  (core/timeparse.py:221)
    alt intent=reschedule
        IB->>DB: status=reschedule_pending  (service/inbound_email.py:223-225)
    else intent=decline
        IB->>DB: status=declined（终态）
    else intent=confirm
        IB->>DB: status=completed（终态）
    end
    IB->>FS: notify_supervisor_about_candidate(...)（带"下一步该怎么做"的提示语）  (:262-287)
```

事实：
- 意图判定是**规则优先**：`decline` 关键字先于 `reschedule` 先于 `confirm`（`service/extraction.py:103-111`）；LLM 只在可用时改写为更准的 intent，且"模型说 other 但规则是非 other"时保留规则（`service/extraction.py:331-332`）。
- 改期确认由主管触发：`_handle_reschedule_decision` 支持"直接给新时间"或"同意"（沿用候选人提议的时间，从 `candidate.reply.reschedule` 事件里读回，`service/feedback.py:687-697`）。
- 改期确认邮件走 `send_reschedule_confirmation`，复用同一线程的 `In-Reply-To/References`（`service/notification.py:849-864`）。

---

## 13. 异常、重试、幂等与人工介入

| 机制 | 实现 | 证据 |
|---|---|---|
| 单封邮件失败不阻断 | `poll_once` 内 try/except，`failed += 1`，继续下一封 | `service/inbound_email.py:90-106` |
| 单份简历解析崩溃不阻断 | 子进程隔离 + 降级空文本 | `core/safe_process.py:97-186`、`service/ingestion.py:332-362` |
| 单个职位分发失败不阻断 | `_dispatch_position` 外层 try/except，返回 `ok=False` | `service/notification.py:104-114` |
| 编排单步失败不阻断 | 配置同步/轮询/分发/补发各自 try/except，错误进 `RunCycleResponse.errors` | `service/orchestrator.py:87-134` |
| 邮件幂等 | 同 `Message-ID` 已 `sent/mocked` → 跳过 | `repository/message.py:35-51`、`service/notification.py:378-384` |
| 飞书幂等 | 服务端 message_id 无法预生成 → 按「收件人+正文」精确匹配 | `repository/message.py:53-71`、`service/notification.py:774-821` |
| 失败补发（不立即重试） | 下一轮 `retry_failed_interview_notices()`（按原 Message-ID）/ `retry_failed_feishu_messages()`；改期确认邮件**不自动补发**（防越权） | `service/notification.py:729-772`（`:749-755` 显式跳过"调整"主题） |
| 未归位人工介入 | 停 `received` + `candidate.unmatched_position` 事件 + 飞书告警 | `service/ingestion.py:364-452` |
| 主管无法识别 | 记 `feedback.supervisor_unknown` 并忽略 | `service/feedback.py:119-129` |
| 无法归类的入站邮件 | 记 `inbound.unmatched` 事件并忽略 | `service/inbound_email.py:157-181` |
| 审计 | 三张表：`candidates` / `messages` / `events`，事件动作名即状态机证据 | `model/*.py`、`api/v1/recruitment.py:116-168` |

**未发现实现**（明确列出，不补画）：
- 固定次数重试 / 指数退避（`AGENT.md:20` 明确不做）。
- 死信队列 / 失败任务人工审核台（只有 CLI + HTTP 查询）。
- 邮件"已读/未读"状态回写、IMAP 移动/标记已读（`_live_poll` 只 `fetch`，不 `STORE \Seen`）。
- 面试提醒（面试前一天再发一次）、候选人接受后的日程邀请（ICS）。
