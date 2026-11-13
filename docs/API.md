# API.md｜HTTP 接口文档

服务默认监听 `http://127.0.0.1:8000`，全部 JSON（除简历上传为 multipart）。交互式文档：`/docs`。

**这里只有运维/联调接口，没有面向终端用户的页面**——产品界面是飞书对话与邮件。

- 统一前缀：`/api/v1`（健康检查除外）
- 统一错误信封：`{"error": {"code", "message", "request_id", "details"}}`
- 响应头带 `X-Request-ID`；请求可自带同名头透传，便于串联日志

| 分组 | 前缀 | 文件 |
|---|---|---|
| 招聘主流程 | `/api/v1/recruitment` | `api/v1/recruitment.py` |
| 飞书回调 | `/api/v1/feishu` | `api/v1/feishu.py` |
| 邮件 | `/api/v1/email` | `api/v1/email.py` |
| 运维 | `/health` | `factory/app.py` |
| 运营视图 | `/api/v1/ops` | `api/v1/ops.py` |

**鉴权（2026-11-14 起）**：除 `GET /health` 与 `POST /api/v1/feishu/webhook` 外，
`/api/v1/*` 全部要求令牌，两种写法等价：

```bash
curl -H "X-API-Token: $API_TOKEN"  ...
curl -H "Authorization: Bearer $API_TOKEN"  ...
```

不带令牌 → `401 unauthorized`。令牌在 `.env` 的 `API_TOKEN`；留空时服务仍能起，
但只在 `ENVIRONMENT=dev` 下放行，并在启动日志打 `security.api_token_missing`。
飞书回调不走这个令牌，它靠 `X-Lark-Signature`（`sha256(timestamp+nonce+encrypt_key+body)`）
加 `verification_token` 校验，两者至少满足一种；生产环境（非 dev）两者都没配时**一律拒收**。

---

## 一、招聘主流程

### 1. 触发一轮完整闭环

`POST /api/v1/recruitment/run`

定时任务的同一入口：同步配置 → 收信（简历 + 回复）→ 解析筛选 → 按职位打包发主管 → 幂等补发上轮失败消息。

请求体：无（可传空 `{}`）

```json
{}
```

响应 `200`：

```json
{
  "cycle_id": 202611120900,
  "triggered_at": "2026-11-12T01:00:03.512000",
  "ingested": 6,
  "parsed": 6,
  "screened_passed": 5,
  "screened_rejected": 1,
  "duplicates": 0,
  "packages": [
    {
      "position_code": "backend_senior",
      "position_title": "高级后端工程师",
      "supervisor": "张伟",
      "received_count": 3,
      "passed_count": 2,
      "candidate_names": ["陈昊然", "林思远"],
      "package_path": "var/packages/202611120900/backend_senior.zip",
      "feishu_message_id": "om_xxx",
      "email_message_id": "<xxx@xingye-test.com>",
      "ok": true,
      "error": ""
    }
  ],
  "errors": []
}
```

| 字段 | 说明 |
|---|---|
| `cycle_id` | 本轮编号，`YYYYMMDDHHMM`（本地时间） |
| `ingested` / `parsed` | 本轮入库数 / 已解析数 |
| `screened_passed` / `screened_rejected` | 通过 / 淘汰数 |
| `duplicates` | 重复投递数（只追加事件，不覆盖首条） |
| `packages[]` | 每个职位的分发结果，单职位失败只体现在该条的 `ok=false` |
| `errors[]` | 非致命错误汇总，主流程已尽量跑完 |

### 2. 手动导入一份简历文件

`POST /api/v1/recruitment/ingest`

`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | file | 是 | `.pdf` / `.docx` / `.txt` |
| `position_code` | string | 否 | 指定职位；不填则按关键词自动归位 |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/recruitment/ingest \
  -F "file=@测试简历/格式样本/陈昊然-简历.pdf" \
  -F "position_code=backend_senior"
```

响应 `200`：

```json
{
  "candidate_id": 21,
  "name": "陈昊然",
  "position_code": "backend_senior",
  "status": "screened_passed",
  "score": 92.5,
  "is_duplicate": false,
  "message": "已入库并完成筛选"
}
```

### 3. 手动导入简历纯文本

`POST /api/v1/recruitment/ingest/text`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | 是 | 简历正文，至少 20 字 |
| `filename` | string | 否 | 默认 `manual-resume.txt` |
| `position_code` | string | 否 | 指定职位 |

```json
{
  "text": "姓名：王小明\n电话：13800138000\n邮箱：wxm@example.com\n教育背景：本科 浙江大学 计算机\n工作年限：6 年\n技能：Python, FastAPI, Redis",
  "filename": "wangxiaoming.txt",
  "position_code": "backend_senior"
}
```

响应同第 2 个接口。

### 4. 查询候选人列表

`GET /api/v1/recruitment/candidates`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `status` | string | — | 状态过滤，见 AGENT.md 状态机 |
| `position_code` | string | — | 职位过滤 |
| `name` | string | — | 姓名模糊匹配 |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | 分页偏移 |

```json
{
  "total": 1,
  "items": [
    {
      "id": 21,
      "name": "陈昊然",
      "email": "chenhaoran@example.com",
      "phone": "13900001111",
      "position_code": "backend_senior",
      "department": "技术部",
      "education_level": "本科",
      "school": "浙江大学",
      "major": "计算机科学与技术",
      "years_experience": 6.0,
      "skills": ["Python", "FastAPI", "Redis", "PostgreSQL"],
      "project_highlights": ["高并发订单系统", "微服务重构"],
      "score": 92.5,
      "status": "screened_passed",
      "status_label": "筛选通过",
      "hard_failed": [],
      "is_duplicate": false,
      "interview_time": null,
      "interview_time_display": "",
      "resume_filename": "陈昊然-简历.pdf",
      "created_at": "2026-11-12T01:00:04.120000"
    }
  ]
}
```

### 5. 候选人详情

`GET /api/v1/recruitment/candidates/{candidate_id}`

响应：单个候选人对象（结构同上）。不存在时 `404`：

```json
{"error": {"code": "not_found", "message": "候选人 999 不存在", "request_id": "b1c2...", "details": {}}}
```

### 6. 单人事件流水

`GET /api/v1/recruitment/candidates/{candidate_id}/events`

```json
[
  {
    "id": 88,
    "action": "candidate.screened",
    "actor": "system",
    "object_type": "candidate",
    "object_id": 21,
    "from_state": "parsed",
    "to_state": "screened_passed",
    "detail": {"score": 92.5, "threshold": 60},
    "note": "",
    "occurred_at": "2026-11-12T01:00:05.004000"
  }
]
```

### 7. 全局事件

`GET /api/v1/recruitment/events`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `limit` | int | 100 | 1–500 |
| `action` | string | — | 按动作过滤，如 `cycle.completed` |

响应：事件对象数组（结构同上）。

### 8. 收发留痕

`GET /api/v1/recruitment/messages`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `channel` | string | — | `feishu` / `email` |
| `direction` | string | — | `outbound` / `inbound` |
| `limit` | int | 50 | 1–200 |

```bash
curl "http://127.0.0.1:8000/api/v1/recruitment/messages?channel=email&direction=outbound"
```

```json
[
  {
    "id": 41,
    "channel": "email",
    "direction": "outbound",
    "sender": "hr@xingye-test.com",
    "recipient": "chenhaoran@example.com",
    "subject": "【面试通知】高级后端工程师｜星野科技",
    "body": "陈昊然 您好：……",
    "external_id": "<202611120900-abc@xingye-test.com>",
    "status": "sent",
    "candidate_id": 21,
    "position_id": 1,
    "parsed": {},
    "error": "",
    "created_at": "2026-11-12T02:10:00.000000"
  }
]
```

### 9. 在招职位与主管映射

`GET /api/v1/recruitment/positions`

```json
[
  {
    "code": "backend_senior",
    "title": "高级后端工程师",
    "department": "技术部",
    "headcount": 2,
    "threshold": 60.0,
    "rule_version": "v1",
    "supervisor_name": "张伟",
    "supervisor_title": "张总",
    "supervisor_email": "zhangwei@xingye-test.com",
    "active": true
  }
]
```

---

## 二、飞书回调

### 10. 飞书事件入口

`POST /api/v1/feishu/webhook`

飞书后台的事件订阅地址，支持两种事件：

| event_type | 场景 |
|---|---|
| `im.message.receive_v1` | 主管在飞书里自由文本回复名单与时间 |
| `card.action.trigger` | 主管点击卡片按钮（与文本走同一套解析） |

配置回调地址时飞书会先发挑战，原样回 `challenge`：

```json
{"challenge": "7d8f..."}
```

收到事件后：

```json
{"ok": true, "event_type": "im.message.receive_v1", "handled": true,
 "detail": {"action": "interview_scheduled", "names": ["陈昊然", "林思远"], "interview_time": "2026-11-18 14:00", "reply": "已通知 2 位候选人"}}
```

| 字段 | 说明 |
|---|---|
| `handled` | `false` 表示消息未进入业务（非文本消息、非登记主管等） |
| `detail.reply` | 机器人回给主管的文本前 200 字 |

> 本地开发没有公网地址时，用 `scripts/feishu_ws_bridge.py`（或双击 `启动飞书长连接.bat`）把长连接事件转发到这个接口。

---

## 三、邮件

### 11. 手动触发一次收信

`POST /api/v1/email/poll`

拉一次收件箱，按「候选人回信 → 主管反馈 → 简历附件 → 丢弃」分流。

```json
{
  "processed": 2,
  "ignored": 1,
  "failed": 0,
  "details": [
    {"kind": "candidate_reply", "handled": true, "from": "chenhaoran@example.com"},
    {"kind": "resume", "handled": true, "from": "linny@example.com", "duplicates": 0}
  ],
  "errors": []
}
```

### 12. 邮件通道自检

`POST /api/v1/email/test`

| 字段 | 类型 | 必填 | 默认 |
|---|---|---|---|
| `to` | string | 是 | — |
| `subject` | string | 否 | `【测试】ai-hr 邮件通道自检` |
| `body` | string | 否 | 自检说明 |

```json
{"to": "you@example.com"}
```

```json
{"ok": true, "mode": "mock", "message_id": "<202611121200-abc@local>", "error": ""}
```

`mode=mock` 时不会真的发信，`.eml` 落在 `var/outbox/mail/`。

### 13. 模拟一封入站邮件

`POST /api/v1/email/simulate-inbound`

不连 IMAP 也能测「回信 → 解析 → 发通知」：把一封信直接投进处理管道。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `from_address` | string | 是 | 发件人，需与已登记的主管/候选人邮箱匹配 |
| `body` | string | 是 | 正文 |
| `subject` | string | 否 | 主题 |
| `in_reply_to` | string | 否 | 原邮件 Message-ID，用于线程匹配 |
| `references` | string[] | 否 | 引用链 |
| `to_addresses` | string[] | 否 | 默认 HR 邮箱 |
| `message_id` | string | 否 | 不填自动生成 |

```json
{
  "from_address": "chenhaoran@example.com",
  "subject": "Re: 【面试通知】高级后端工程师",
  "body": "下周二下午有会，能否改到周三上午 10 点？",
  "in_reply_to": "<202611120900-abc@xingye-test.com>"
}
```

```json
{"processed": 1, "ignored": 0, "failed": 0,
 "details": [{"kind": "candidate_reply", "handled": true, "from": "chenhaoran@example.com", "intent": "reschedule"}],
 "errors": []}
```

---

## 四、运维

### 14. 健康检查

`GET /health`

```json
{
  "status": "ok",
  "app": "ai-hr-robot",
  "environment": "dev",
  "time": "2026-11-12T09:00:00+08:00",
  "scheduler_running": true,
  "components": {
    "database": "postgresql+asyncpg",
    "feishu_mode": "mock",
    "mail_mode": "mock",
    "llm_enabled": false,
    "company": "星野科技（测试公司）",
    "positions": 5,
    "auth": "enabled",
    "inbound_poll_seconds": 300,
    "schema_revision": "0004_message_lookup",
    "config_error": ""
  }
}
```

- `auth`：`enabled` = 配了 `API_TOKEN`；`disabled` = 没配（仅 dev 放行）。
- `inbound_poll_seconds`：入站轮询间隔；`0` 表示关闭定时拉信（只收 webhook/手动触发）。
  改这个值要重启服务，运行期不生效。
- `schema_revision`：库当前处在哪个迁移版本。比对这个值与 `alembic heads` 就知道
  要不要升级；`unknown` 表示读不到（库没起 / 没有 `alembic_version` 表）。
  迁移失败时服务仍会起来，这里会停在旧版本号上，不会假装成功。

`status=degraded` 表示 `positions.yaml` 读取/校验失败（`config_error` 有原因），服务还活着但业务跑不了。

---

## 五、运营视图

> 产品明确「不做管理后台」，所以这三个接口只输出**只读**数据，给运维、日报、监控用。
> 三个都要求令牌。

### 15. 招聘漏斗

`GET /api/v1/recruitment/stats?days=30&daily=true`

| 参数 | 默认 | 说明 |
|---|---|---|
| `days` | 30 | 统计窗口，按候选人 `created_at` 落在最近 N 天 |
| `daily` | false | 是否附上按天流水（`received` / `passed` / `rejected`） |

```json
{
  "since": "2026-10-14T20:23:15",
  "until": "2026-11-13T20:23:15",
  "stage_snapshot": {
    "interview_notified": 4,
    "supervisor_feedback": 1,
    "screened_rejected": 17,
    "dispatched": 2
  },
  "by_position": [
    {"position_code": "python_developer", "interview_notified": 3, "screened_rejected": 15}
  ],
  "outbound": {"feishu.sent": 9, "email.sent": 7},
  "pending_approval": 0,
  "duplicates_in_window": 0,
  "daily": [{"day": "2026-11-12", "received": 24, "passed": 7, "rejected": 17}]
}
```

口径说明（这几个词很容易被理解错）：

- `stage_snapshot` 是**当前状态分布**，不是历史累计；`is_duplicate=true` 的重复投递**不计入**，
  否则「同一人投了 3 次」会把过筛率算低。
- `by_position` 只列有数据的职位，不补零。
- `outbound` 只统计 `sent` / `mocked`，失败的不算「发了」。
- `since` / `until` 是 **UTC naive**（与库内一致），展示给 HR 时按 `TIMEZONE` 转换。

`GET /api/v1/ops/funnel` 是同口径的短窗口版（默认 7 天），字段名一致，省得每次传参。

### 16. Prometheus 指标

`GET /api/v1/ops/metrics?days=30`

返回 `text/plain`（Prometheus exposition format），可直接接 `prometheus.yml` 的 `scrape_configs`：

```text
# HELP hr_candidates_total 各状态候选人总数
# TYPE hr_candidates_total gauge
hr_candidates_total{status="screened_rejected"} 17
# HELP hr_messages_total 各渠道/方向/状态的留痕条数
# TYPE hr_messages_total counter
hr_messages_total{channel="email",direction="inbound",status="received"} 40
# HELP hr_pending_approval 待 HR 确认的面试通知数
# TYPE hr_pending_approval gauge
hr_pending_approval 0
```

现有指标：`hr_candidates_total`、`hr_messages_total`、`hr_pending_approval`、
`hr_outbound_failed`、`hr_screened_rejected_total`。

> 为什么是这几个：`hr_outbound_failed` 涨 = 发信在失败；`hr_pending_approval` 长期不为 0
> = HR 忘了确认，候选人一直收不到通知。这两个是最值得告警的。

---

## 六、错误码

| code | HTTP | 触发场景 |
|---|---|---|
| `validation_failed` | 400 | 参数缺失、上传空文件、简历无法解析入库 |
| `unauthorized` | 401 | 缺令牌 / 令牌不对（除 `/health` 与飞书回调外的所有接口） |
| `not_found` | 404 | 候选人 / 资源不存在 |
| `conflict` | 409 | 状态冲突 |
| `configuration_error` | 500 | 配置缺失或非法（如飞书加密推送未支持） |
| `external_service_error` | 502 | 飞书 / 邮件等外部系统报错 |
| `llm_unavailable` | 503 | 主模型与全部 fallback 都失败 |
| `mailbox_unavailable` | 503 | SMTP / IMAP 连不上 |
| `internal_error` | 500 | 未预期异常 |

```json
{
  "error": {
    "code": "validation_failed",
    "message": "上传文件为空",
    "request_id": "e3f1c0a9d2b4",
    "details": {}
  }
}
```

排查时拿 `request_id` 去日志里搜即可定位到那一条请求的全部输出。
