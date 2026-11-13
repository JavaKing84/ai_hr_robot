# 06 · HTTP 接口与外部集成

路由总挂载：`factory/routers.py:11` `app.include_router(api_router, prefix="/api/v1")`；`/health` 在 `factory/app.py:49` 单独注册（不在 `/api/v1` 下）。OpenAPI 文档：`/docs`（`redoc_url=None`，`factory/app.py:41-42`）。

---

## 1. HTTP 路由清单

### 1.1 `/api/v1/recruitment`（`api/v1/recruitment.py`）

| 方法 | 路径 | 请求 | 响应模型 | 下游 |
|---|---|---|---|---|
| POST | `/api/v1/recruitment/run` | 无 body | `RunCycleResponse` | `services.orchestrator.run_daily_cycle()`（`:31-33`） |
| POST | `/api/v1/recruitment/ingest` | `multipart/form-data`：`file`（pdf/docx/txt）、`position_code`（可选 Form） | `IngestResponse` | `services.ingestion.ingest_resume_bytes(source="manual")`（`:36-51`）；空文件抛 `ValidationFailed` |
| POST | `/api/v1/recruitment/ingest/text` | `IngestTextRequest{text(>=20 字符), filename, position_code?}` | `IngestResponse` | 同上，`source="manual-text"`（`:54-64`） |
| GET | `/api/v1/recruitment/candidates` | query：`status`、`position_code`、`name`、`limit`(1-200,默认50)、`offset` | `CandidateListResponse{total, items[]}` | `CandidateRepository.search()`（`:67-83`，**api 层直接调 repository**） |
| GET | `/api/v1/recruitment/candidates/{candidate_id}` | path int | `CandidateRead` | `session.get(Candidate)` → 不存在抛 `NotFoundError`（`:86-91`） |
| GET | `/api/v1/recruitment/candidates/{id}/events` | path int | `list[EventRead]` | `EventRepository.list_for_object("candidate", id)`（`:94-113`） |
| GET | `/api/v1/recruitment/events` | query：`limit`(1-500)、`action` | `list[EventRead]` | `EventRepository.list_recent()`（`:116-137`） |
| GET | `/api/v1/recruitment/messages` | query：`channel`、`direction`、`limit`(1-200) | `list[MessageRead]` | `MessageRepository.list_recent()`（`:140-168`） |
| GET | `/api/v1/recruitment/positions` | 无 | `list[PositionRead]` | `PositionRepository.list_active()`（`:171-188`） |

### 1.2 `/api/v1/feishu`（`api/v1/feishu.py`）

| 方法 | 路径 | 请求 | 响应 | 行为 |
|---|---|---|---|---|
| POST | `/api/v1/feishu/webhook` | 飞书事件原始 JSON（可 `encrypt` 包装） | `FeishuWebhookResponse{ok, event_type, handled, detail}`；挑战请求返回 `{"challenge": ...}` | `decode_webhook` → 挑战应答 → `normalize_event` → `feedback.handle()`（`:26-52`） |

### 1.3 `/api/v1/email`（`api/v1/email.py`）

| 方法 | 路径 | 请求 | 响应 | 行为 |
|---|---|---|---|---|
| POST | `/api/v1/email/poll` | 无 | `EmailPollResponse{processed, ignored, failed, details, errors}` | `orchestrator.poll_inbound()`（`:25-27`） |
| POST | `/api/v1/email/test` | `EmailTestRequest{to, subject, body}` | `EmailTestResponse{ok, mode, message_id, error}` | 直接调 `MailboxClient.send()`，**不落库不算业务**（`:30-42`） |
| POST | `/api/v1/email/simulate-inbound` | `SimulateInboundEmailRequest{from_address, subject, body, in_reply_to?, references?, to_addresses?, message_id?}` | `EmailPollResponse` | 构造 `InboundEmail` → `inbound.handle_email()`，**不依赖真实 IMAP**（`:45-76`） |

### 1.4 运维

| 方法 | 路径 | 响应 | 内容 |
|---|---|---|---|
| GET | `/health` | `HealthResponse` | `status`（配置坏时 `degraded`）、`components.database`（只回 scheme）、`feishu_mode`、`mail_mode`、`llm_enabled`、`company`、`positions` 数量、`config_error`、`scheduler_running`（`factory/app.py:49-68`） |
| GET | `/docs` | Swagger UI | `factory/app.py:41` |

### 1.5 错误信封（所有路由统一）

```json
{"error": {"code": "...", "message": "...", "request_id": "...", "details": {}}}
```
实现 `middlewares/exception.py:20-63`；`AppError` 子类的 `code/status_code`（`core/exceptions.py:31-63`）：

| 异常 | code | HTTP |
|---|---|---|
| ValidationFailed | validation_failed | 400 |
| NotFoundError | not_found | 404 |
| ConflictError | conflict | 409 |
| ConfigurationError | configuration_error | 500 |
| ExternalServiceError | external_service_error | 502 |
| LLMUnavailable | llm_unavailable | 503 |
| MailboxUnavailable | mailbox_unavailable | 503 |
| Pydantic 校验失败 | validation_error | 422 |
| 未捕获异常 | internal_error | 500（不泄露堆栈，靠 request_id 追） |

> 注意：`service/` 层的失败**多数不抛上述异常**，而是被 service 内部降级成 `ok=False`/`errors[]`（例如 `dispatch_cycle` 返回 `PackageDispatchResult.ok=false`）。因此 `/recruitment/run` 即便"什么都没发出去"也可能返回 200，需要读 `errors` 与 `packages[].error`。

---

## 2. 飞书集成

| 项 | 实现 | 证据 |
|---|---|---|
| SDK | `lark_oapi`，`lark.Client.builder().app_id().app_secret()` | `client/feishu.py:46-65` |
| 出站通道选择 | `FEISHU_PUSH_TARGET=group` 且（有 webhook 或 chat_id）→ 群；否则按 `open_id` 私聊 | `config/settings.py:213-224`、`client/feishu.py:71-82` |
| 群文本 | 优先应用身份 `im/v1/messages`（`receive_id_type=chat_id`），无 chat_id 才回退 webhook POST | `client/feishu.py:112-122` |
| 群文件 | 必须应用身份：`im/v1/files` 上传拿 `file_key` → `im/v1/messages`（`msg_type=file`）；webhook 不支持文件 | `client/feishu.py:157-243` |
| 上传失败兜底 | 在群里发一条"压缩包已生成在 var/packages + 要开 `im:resource:upload`"的提示 | `client/feishu.py:245-258` |
| webhook 成功判定 | 同时认 `{"code":0}` 与新老两种结构 `{"StatusCode":0}` | `client/feishu.py:260-284` |
| 入站事件类型 | `im.message.receive_v1`（自由文本）、`card.action.trigger`（卡片按钮） | `client/feishu.py:442-446` |
| 文本提取 | `text` 直取；`post`（富文本）展平；`mentions` 里的 key 替换成姓名 | `client/feishu.py:563-595` |
| 加解密 | `encrypt` 体用 `lark_oapi.core.utils.AESCipher(ENCRYPT_KEY).decrypt_string()`；**不自行实现 AES** | `client/feishu.py:409-428` |
| 验签 | **仅** verification token 比对，且 token 为空时跳过 | `client/feishu.py:412-415` |
| 下载主管发的文件 | `im/v1/messages/{id}/resources`（`download_resource`） | `client/feishu.py:522-545` |
| 长连接（免公网） | `scripts/feishu_ws_bridge.py` 把 WS 事件转发到本地 webhook；`.bat` 可双击启动 | 脚本 docstring |
| Mock | `FEISHU_MODE=mock` 时返回 `mock-feishu-*` 合成 message_id，不发起网络请求 | `client/feishu.py:104-110,291-297` |

**真实联调所需权限**（`AGENT.md:32` 文档描述，未在本机验证）：`im:message`、`im:resource:upload`（发文件必需）、`im:message:send_as_bot`、按手机号/邮箱取用户 ID、订阅 `im.message.receive_v1`、应用版本已发布。
**当前缺失**：`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY` 均为空（回调用明文，且无校验）。

---

## 3. 邮件集成

| 项 | 实现 | 证据 |
|---|---|---|
| 发送 | `aiosmtplib.send(...)`，`use_tls`/`start_tls` 二选一，timeout=30 | `client/mailbox.py:193-211` |
| 收信 | `imaplib`（SSL 可选），登录后 `ID` 命令（网易系必需）→ `select` → `uid search UID last+1:*` → `uid fetch RFC822` | `client/mailbox.py:246-306` |
| 增量水位 | `var/imap_state.json` 存 `last_uid`，只进不退 | `client/mailbox.py:266-273,303-304` |
| Mock 发件 | 写 `var/outbox/mail/<时间戳>-<sha1前12>.eml` | `client/mailbox.py:181-191` |
| Mock 收件 | 读 `var/mock_inbox/*.eml`，用 `.state.json` 的 **mtime** 判"是否处理过" | `client/mailbox.py:222-244` |
| Message-ID 生成 | `<uuid.hex.uuid8@域名>` | `client/mailbox.py:88-89` |
| 线程头 | 发送时写 `Message-ID` / `In-Reply-To` / `References` / `Reply-To` | `client/mailbox.py:135-179` |
| 回复路由地址 | `reply+<token>@<HR 邮箱域名>` | `client/mailbox.py:91-93` |
| 入站解析 | `parse_email_bytes`：正文 text/plain 优先，缺失则 HTML 去标签；附件白名单 `.pdf/.doc/.docx/.txt/.md/.zip/.png/.jpg/.jpeg` | `client/mailbox.py:321-422` |
| 线程匹配 | In-Reply-To/References → `reply+token` → 主管邮箱 → 候选人邮箱；带简历附件跳过"按发件人认人" | `service/email_threading.py:92-178` |
| 附件大小限制 | **无显式限制**（只按后缀过滤，依赖 IMAP 单封大小） | `client/mailbox.py:367-386` |

**当前缺失/注意**：邮件处于 live（163），SMTP/IMAP 均已配置；未实现"标记已读/移动到文件夹"，因此拉过的邮件仍留在 INBOX。

---

## 4. LLM 集成

### 4.1 调用路径

```
service/* → LLMClient.complete_json / complete_text / safe_complete_json
          → _candidate_providers()（LLM_PROVIDERS 链；为空则 LLM_MODEL + FALLBACK）
          → _complete_raw()：litellm.acompletion（首选） / httpx POST {base}/chat/completions（兜底）
          → _content_of()：content 为空时读 reasoning_content / reasoning
          → _parse_json()：去 ```json 围栏 → json.loads → 失败则 extract_json_objects() 从后往前试
```
证据：`client/llm.py:130-303`。

### 4.2 五个提示词节点（`prompt/`）

| 节点 | system | max_tokens | 调用方 |
|---|---|---|---|
| 简历结构化 | `RESUME_SYSTEM` + `build_resume_prompt` | 1500（默认） | `service/extraction.py:199-203` |
| 面试安排抽取 | `INTERVIEW_SYSTEM` + few-shot | 1500 | `service/extraction.py:246-256` |
| 候选人回信意图 | `REPLY_SYSTEM` + few-shot | 1500 | `service/extraction.py:310-319` |
| 语义分 | `SEMANTIC_SYSTEM` | 1800 | `service/screening.py:390-402` |
| 深度解析 | `DEEP_ANALYSIS_SYSTEM` | 1800 | `service/screening.py:377-389` |
| 意图路由（Agent） | `ROUTER_SYSTEM` + few-shot | 1500, temperature=0 | `service/agent.py:132-135` |
| 作答（Agent） | `ANSWER_SYSTEM` | 1200, temperature=0.3 | `service/agent.py:230-243` |

### 4.3 失败处理（分层，全都不抛到业务）

| 失败形态 | 处理 | 证据 |
|---|---|---|
| 单个供应商异常/超时 | 记 `llm.provider_failed`，换下一家 | `client/llm.py:341-355` |
| 返回不是合法 JSON | **同一家**把 max_tokens 翻倍再试一次；仍失败才换家 | `client/llm.py:323-326,343` |
| JSON 埋在散文/思考里 | 括号配对切候选对象，从后往前试 | `client/llm.py:55-101` |
| 思考型模型 content 为 null | 读 `reasoning_content`/`reasoning`；配置 `disable_thinking` 时用 `chat_template_kwargs.enable_thinking=false` 关掉思考 | `client/llm.py:104-116,186-187,242-246,263-271` |
| 全部供应商挂 | 抛 `LLMUnavailable`；`extraction` 回落规则抽取、`screening` 回落规则分、`agent` 静默退场 | `client/llm.py:357-361`、`service/extraction.py:204-209`、`service/screening.py:421-422`、`service/agent.py:103-104,136-138` |
| 静默版 | `safe_complete_json` 永不抛，返回 `None` | `client/llm.py:438-457` |

### 4.4 本机实际 LLM 形态（运行证实，不记录 key）

- `LLM_MODE=live`，`LLM_PROVIDERS` 解析出 **3 家**（顺序即降级顺序：两个学院自建 OpenAI 兼容端点 + DeepSeek 官方）。`LLM_TIMEOUT_SECONDS=25`。
- `OPENAI_API_KEY` 为空 —— 因为走的是 `LLM_PROVIDERS`，这个键不参与（`AGENT.md:102` 的提示"key 统一写这里"仅对单供应商模式成立）。
- 模型名依赖供应商 `/models` 的实际 ID（`.env.example:47-51` 有实测记录）。

---

## 5. Mock / Real 开关总表

| 集成 | 开关 | mock 行为 | live 行为 |
|---|---|---|---|
| 飞书 | `FEISHU_MODE` | 合成 message_id，只落 `messages` 表 | 真调 `lark_oapi`；群模式另有 `CHAT_ID`/`WEBHOOK_URL` 两条子通道 |
| 邮件 | `MAIL_MODE`（多一个 `off`） | 发信写 `.eml`，收信读 `mock_inbox` | 真 SMTP/IMAP |
| LLM | `LLM_MODE`（`auto` 看有无 key/链） | 完全规则路径（`tests` 强制 off） | 逐供应商调用 |

三态语义（`AGENT.md:37`）：`mock` **不是假数据分支**——出站内容仍全量落库落盘，入站仍用真实报文格式，只是不真发。

---

## 6. 真实联调所需但当前缺少的配置（只写变量名）

| 变量 | 现状 | 缺了会怎样 |
|---|---|---|
| `FEISHU_VERIFICATION_TOKEN` | 空 | 回调不做 token 校验（安全问题，不影响功能） |
| `FEISHU_ENCRYPT_KEY` | 空 | 若飞书后台开启加密推送，`_decrypt` 会抛 `configuration_error` |
| `LLM_FALLBACK_MODELS` | 空 | 无影响（已用 `LLM_PROVIDERS` 链） |
| `OPENAI_API_KEY` | 空 | 无影响（已用 `LLM_PROVIDERS`） |
| `FEISHU_MENTION_OPEN_IDS` | 空 | 群消息不 @ 人，主管可能漏看 |
| `INBOUND_POLL_SECONDS` | 0 | 邮件类反馈不实时（次日 cron 才处理） |
