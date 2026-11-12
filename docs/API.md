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
    "database": "sqlite+aiosqlite",
    "feishu_mode": "mock",
    "mail_mode": "mock",
    "llm_enabled": false,
    "company": "星野科技（测试公司）",
    "positions": 4,
    "config_error": ""
  }
}
```

`status=degraded` 表示 `positions.yaml` 读取/校验失败（`config_error` 有原因），服务还活着但业务跑不了。

---

## 五、错误码

| code | HTTP | 触发场景 |
|---|---|---|
| `validation_failed` | 400 | 参数缺失、上传空文件、简历无法解析入库 |
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
