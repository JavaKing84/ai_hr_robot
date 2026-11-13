# SOURCE EVIDENCE · 关键结论的源码证据集

- 生成时间：2026-11-14T01:12+08:00
- 分支 / commit：`develop` / `bf2cc81fb7b18aafc45c2e47ae844def216e4b8e`
- 用途：给 ChatGPT 网页端做二次分析时，**不必回仓库也能核对结论**
- 纪律：每条给出「结论 → 相对路径:行 → 片段（逐字，未改写）→ 判据 → 复现命令」。片段一律从磁盘重新读取后复制，不凭记忆书写。
- 判据含义：`直接证实`＝代码本身如此；`运行证实`＝本次实际执行得到；`文档描述`＝仅文档这么写；`推断`＝代码未写死的设计意图

> 片段只保留解释问题所需的最小行数；省略处用 `…` 标注，未改任何字符。

---

## 1. 构建与启动

### E-01 容器启动命令指向一个不存在的文件
- **路径**：`Dockerfile:27`（该文件共 27 行，CMD 即最后一行）
- **片段**：
  ```dockerfile
  CMD ["python", "main.py"]
  ```
- **判据**：直接证实（CMD 存在）+ 直接证实（根目录无 `main.py`）
- **复现**：
  ```bash
  wc -l Dockerfile && tail -1 Dockerfile && ls *.py
  ```
  `ls *.py` 返回 `No such file or directory`。

### E-02 应用真实入口是工厂函数，不是 `main.py`
- **路径**：`factory/app.py:31`（`def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:`）、`api/v1/router.py:7`
- **片段**：
  ```python
  api_router = APIRouter()
  ```
- **判据**：直接证实
- **说明**：`create_app()` 聚合 `factory/routers.py` + `factory/middleware.py`；因此修正 CMD 的等价写法是 `uvicorn factory.app:create_app --factory`。

### E-03 启动时用 `create_all` 建表（与 Alembic 并存）
- **路径**：`factory/lifespan.py:90`
- **片段**：
  ```python
  await init_models(container.engine)
  ```
- **判据**：直接证实
- **配套事实**：`core/db.py:35-44` `init_models` 内部执行 `Base.metadata.create_all`；同仓库另有 `alembic.ini` + `migrations/versions/0001_initial.py`。
- **复现**：`grep -n "init_models" factory/lifespan.py core/db.py`

### E-04 调度器：每日 cron + 可选入站轮询，均 `coalesce=True, max_instances=1`
- **路径**：`factory/lifespan.py:48-63`（每日任务）、`:65-75`（轮询任务）
- **片段**：
  ```python
  cron = settings.schedule_cron or container.company_config.schedule.cron or "0 9 * * *"
  scheduler.add_job(
      run_daily_job,
      CronTrigger.from_crontab(cron, timezone=settings.timezone),
      args=[container],
      id=DAILY_JOB_ID,
      name="每日招聘闭环",
      replace_existing=True,
      coalesce=True,
      max_instances=1,
      misfire_grace_time=3600,
  )
  if settings.inbound_poll_seconds > 0:
      scheduler.add_job(
          poll_inbound_job,
          IntervalTrigger(seconds=settings.inbound_poll_seconds, timezone=settings.timezone),
          …
  ```
- **判据**：直接证实
- **注意**：`max_instances=1` 只约束**单进程内**同一 job，多副本仍会并发（对应 `07` G-5）。

---

## 2. 数据模型与状态机

### E-05 状态枚举实际 11 个（文档写 10 个）
- **路径**：`model/candidate.py:20-33`
- **片段**：
  ```python
  class CandidateStatus:
      """状态机常量（简化版，对应产品文档第 6 节）。"""

      RECEIVED = "received"                      # 已收取
      PARSED = "parsed"                          # 已解析
      SCREENED_PASSED = "screened_passed"        # 已筛选（通过）
      SCREENED_REJECTED = "screened_rejected"    # 已筛选（淘汰，不再进入后续）
      DISPATCHED = "dispatched"                  # 已发主管
      SUPERVISOR_FEEDBACK = "supervisor_feedback"  # 主管已反馈（进入面试名单）
      INTERVIEW_NOTIFIED = "interview_notified"   # 已邮件通知
      INTERVIEW_PENDING_CONFIRM = "interview_pending_confirm"  # 面试通知已生成，等 HR 确认后发送
      RESCHEDULE_PENDING = "reschedule_pending"   # 改期中（等主管确认）
      DECLINED = "declined"                       # 候选人放弃
      COMPLETED = "completed"                     # 已完成
  ```
- **判据**：直接证实
- **差异**：`AGENT.md:188`、`docs/design/architecture.md:4,177,209`、`docs/DECISIONS.md:13,40`、`service/ingestion.py:376` 注释均称"10 个冻结"，漏掉 `INTERVIEW_PENDING_CONFIRM`。
- **复现**：`sed -n '20,33p' model/candidate.py`

### E-06 `is_duplicate` / `duplicate_of_id` 只有定义，无任何赋值
- **路径**：`model/candidate.py`（列定义）+ 全仓库检索
- **复现**：
  ```bash
  grep -rn "duplicate_of_id\|is_duplicate" --include=*.py . | grep -v .venv
  ```
  输出仅：`model/candidate.py` 的列定义、`migrations/versions/0001_initial.py` 的列定义、`service/ingestion.py` 读 `IngestOutcome.is_duplicate`（内存字段）。
- **判据**：直接证实（"没有赋值语句"这类阴性结论用全量检索证明）

---

## 3. 收信与去重

### E-07 重复投递不新建行，只追加事件
- **路径**：`service/ingestion.py:239-247`
- **片段**：
  ```python
                  note="重复投递：保留首次记录，本次不覆盖",
              )
              await self.session.commit()
              return IngestOutcome(
                  candidate=duplicate,
                  is_duplicate=True,
                  position_code=duplicate.position_code,
                  note="重复投递，已追加事件",
              )
  ```
- **判据**：直接证实
- **推论**：既然不建行，`is_duplicate`/`duplicate_of_id` 两列永远为默认值（对应 E-06、`07` K-3）。

### E-08 简历原文入库时硬截断 50,000 字符
- **路径**：`service/ingestion.py:264`、`:397`（未匹配简历同样截断）
- **片段**：
  ```python
              resume_text=parsed.text[:50_000],
  ```
- **判据**：直接证实
- **影响**：超长简历的后续语义分/深度抽取只看前 50k（`07` K-7）。

---

## 4. 筛选

### E-09 影子模式下语义分不参与判定，只记录
- **路径**：`service/screening.py:462-465`
- **片段**：
  ```python
              applied = semantic is not None and not self.semantic_shadow
              if semantic is not None and self.semantic_shadow:
                  note = (note + " " if note else "") + "影子模式：语义分仅记录，未参与判定"
  ```
- **判据**：直接证实
- **配套**：`config/settings.py:78-80` 注释解释了原因（阈值 55/60 按纯规则分布定），但**未给退出条件**（对应 `09` Q-11）。

---

## 5. 分发与通知（最高风险区）

### E-10 分发状态不看发送结果就推进
- **路径**：`service/notification.py:175-177`
- **片段**：
  ```python
          for candidate in candidates:
              from_state = candidate.status
              candidate.status = CandidateStatus.DISPATCHED
  ```
- **判据**：直接证实
- **关键上下文**：上一行（`:174`）是 `email_result = await self._send_supervisor_email(...)`，之后**没有任何 `if email_result.ok` / `if feishu_text.ok` 判断**就进入 `for` 循环改状态。
- **影响**：发送失败时主管收不到，但系统认为已发（`07` K-1、`09` Q-8）。

### E-11 面试通知同样无条件置 `interview_notified`
- **路径**：`service/notification.py:422-424`
- **片段**：
  ```python
          from_state = candidate.status
          candidate.status = CandidateStatus.INTERVIEW_NOTIFIED
          candidate.interview_time = interview_time
  ```
- **判据**：直接证实

### E-12 确认闸门默认开启，注释已写明"生产确认过再用"
- **路径**：`config/settings.py:66-69`
- **片段**：
  ```python
      # 面试通知确认闸门（测试期间默认开启）：
      # True = 只挂起、不发送，必须由 HR 人工确认后才投递；
      # False = 主管给出名单后直接发送（生产环境确认过再用）。
      interview_notice_require_confirm: bool = True
  ```
- **判据**：直接证实（这是**文档化过的设计**，不是遗漏；遗留问题是无超时兜底，见 `09` Q-10）

---

## 6. 飞书集成

### E-13 回调：有 AES 解密分支，但 token 校验可被空值绕过，且无签名校验
- **路径**：`client/feishu.py:402-428`（`decode_webhook` + `_decrypt`）
- **片段**：
  ```python
      def decode_webhook(self, body: bytes, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
          """解析 webhook 原始请求体：处理 encrypt 加密体并校验 verification token。"""
          try:
              payload = json.loads(body.decode("utf-8") or "{}")
          except (UnicodeDecodeError, json.JSONDecodeError) as exc:
              raise ConfigurationError("飞书回调请求体不是合法 JSON") from exc

          if "encrypt" in payload:
              payload = self._decrypt(payload["encrypt"])

          token = payload.get("token") or (payload.get("header") or {}).get("token") or ""
          expected = self.settings.feishu_verification_token
          if expected and token and token != expected:
              raise ConfigurationError("飞书回调 verification token 不匹配")
          return payload
  ```
- **判据**：直接证实
- **三条独立结论**（不要混为一谈）：
  1. **有**加密体解密路径：`_decrypt` 走官方 `AESCipher`，需要 `FEISHU_ENCRYPT_KEY`（`:418-428`）。
  2. **无**签名校验：`grep -niE "signature|hmac" client/feishu.py` **空命中**，即不校验 `X-Lark-Signature`。
  3. **token 校验可绕过**：条件是 `expected and token and token != expected`，只要 `expected`（`FEISHU_VERIFICATION_TOKEN`）为空，整个比较被短路 → 明文请求体直接放行。
- **本机实测（仅查键状态，未读取值）**：`FEISHU_VERIFICATION_TOKEN` 与 `FEISHU_ENCRYPT_KEY` 均"存在但为空" → 结论 3 成立，且结论 1 的路径会抛 `ConfigurationError`（无法解密）。
- **影响**：能访问回调 URL 的人可伪造明文事件触发外发（`05` R-1、`09` Q-15）。

---

## 7. 邮件线程与幂等

### E-14 飞书出站幂等靠「收件人 + 正文全等」
- **路径**：`repository/message.py:53-71`
- **片段**：
  ```python
      async def find_delivered_by_content(
          self, *, channel: str, recipient: str, body: str
      ) -> Message | None:
          """内容级幂等查重：飞书 message_id 由服务端生成、发送前不存在，
          因此对飞书出站按「收件人 + 正文精确匹配」判重（应用层查表，§4-A.1a 边界内）。"""
          if not recipient or not body:
              return None
          result = await self.session.execute(
              select(Message)
              .where(
                  Message.channel == channel,
                  Message.direction == Direction.OUTBOUND,
                  Message.recipient == recipient,
                  Message.body == body,
                  Message.status.in_(("sent", "mocked")),
              )
              .limit(1)
          )
  ```
- **判据**：直接证实
- **影响**：正文含时间戳/序号时永远匹配不上 → 失败补发会重复（`07` K-4）。

### E-15 线程匹配用 `LIKE %id%`
- **路径**：`repository/email_thread.py:73`
- **片段**：
  ```python
          conditions += [EmailThread.message_id_index.like(f"%{mid}%") for mid in cleaned]
  ```
- **判据**：直接证实
- **影响**：Message-ID 互为子串时可能误配（`07` K-5，概率低）。

---

## 8. 装配与依赖注入

### E-16 进程级 `Container` + 请求级 `Services`
- **路径**：`core/deps.py:41`（`class Container`）、`:99`（`get_container`）、`:121`（`build_services`）
- **片段**：
  ```python
  def build_services(session: AsyncSession, container: Container | None = None) -> Services:
  ```
- **判据**：直接证实
- **说明**：`Container` 持有 settings/engine/scheduler 等进程级单例；`build_services` 按请求绑定一个 `AsyncSession`，构造各 repository/service。这是"出网只在 `client/`、`service/` 不直接读环境变量"的结构基础。

---

## 9. 阴性证据：接口层没有鉴权、没有限流

> 这类结论必须给"检索命令 + 空输出"，否则无法自证。

### E-17 全部 HTTP 接口无认证依赖
- **复现**：
  ```bash
  grep -rnE "Depends\(.*(auth|token|verify|api_key)|HTTPBearer|Security\(" api/ core/deps.py
  ```
- **输出**：**空**（0 行）。
- **旁证**：`factory/middleware.py:16-18` 只注册两个中间件：
  ```python
  def configure_middlewares(app: FastAPI) -> None:
      app.add_middleware(AccessLogMiddleware)
      app.add_middleware(RequestIdMiddleware)
  ```
  `api/v1/router.py:7` 为裸 `APIRouter()`，无 `dependencies=` 全局守卫。
- **判据**：直接证实
- **影响**：能访问端口者即可调用上传简历、触发闭环等接口（`09` Q-13）。

### E-18 无限流实现
- **复现**：
  ```bash
  grep -rniE "rate.?limit|slowapi" --include=*.py . | grep -v .venv
  ```
- **输出**：**空**（0 行）。
- **判据**：直接证实（`09` Q-14）

### E-19 没有 Web 管理界面
- **复现**：
  ```bash
  grep -rn "StaticFiles\|Jinja2Templates\|mount(" --include=*.py . | grep -v .venv
  ```
- **输出**：**空**（0 行）。
- **判据**：直接证实
- **含义**：`api/` 下全部是 JSON 接口，无模板渲染、无静态资源挂载。所有"看数据"的需求只能走 `GET /api/v1/recruitment/*` 或直接查库。

### E-20 解析走子进程 `spawn` 隔离
- **路径**：`core/safe_process.py:112-114`（函数文档串在 `:106` 说明原因）
- **片段**：
  ```python
      context = multiprocessing.get_context("spawn")
      result_queue = context.Queue()
      process = context.Process(
  ```
  `:106` 的文档串原文：`Windows 上使用 ``spawn``，子进程会重新 import 目标模块——因此 ``func`` 不能是闭包或 lambda。`
- **判据**：直接证实
- **含义**：PDF 解析器段错误/内存爆掉不会带走主进程；代价是 `func` 必须是模块级可 pickle 的函数，且启动有固定开销（这解释了为什么单轮收几十份简历时耗时会明显上升）。

---

## 10. 如何验证"我读过的文件"确实被读过

本次知识包的每份文件头部都写了**扫描范围 / 排除范围**。若要抽查：

```bash
# 文件规模（与 00_PROJECT_BRIEF.md 的 105 个 .py / 16,818 行对应；
# 其中业务代码 67 个 / 11,280 行，其余为 tests/scripts/migrations）
git ls-files '*.py' | wc -l
git ls-files '*.py' | xargs wc -l | tail -1

# 是否真有未提交改动（本产物目录除外）
git status --short
```

预期：`?? project-intel/` 为本产物，其余干净。

---

## 证据索引速查

| 证据 | 主题 | 路径:行 |
|---|---|---|
| E-01 | 容器启动失败 | `Dockerfile:27` |
| E-02 | 真实入口 | `api/v1/router.py:7` / `factory/app.py` |
| E-03 | create_all 建表 | `factory/lifespan.py:90` |
| E-04 | 调度器配置 | `factory/lifespan.py:48-75` |
| E-05 | 11 个状态 | `model/candidate.py:20-33` |
| E-06 | 重复列从不赋值 | `model/candidate.py` + 全量检索 |
| E-07 | 重复投递不建行 | `service/ingestion.py:239-247` |
| E-08 | 50k 截断 | `service/ingestion.py:264,397` |
| E-09 | 语义影子 | `service/screening.py:462-465` |
| E-10 | 分发不看结果 | `service/notification.py:175-177` |
| E-11 | 通知不看结果 | `service/notification.py:422-424` |
| E-12 | 确认闸门 | `config/settings.py:66-69` |
| E-13 | 飞书验签缺失 | `client/feishu.py:414` |
| E-14 | 内容级幂等 | `repository/message.py:53-71` |
| E-15 | LIKE 匹配线程 | `repository/email_thread.py:73` |
| E-16 | DI 装配 | `core/deps.py:41,99,121` |
| E-17 | 无鉴权 | `api/`、`core/deps.py`（空命中） |
| E-18 | 无限流 | 全仓库（空命中） |
| E-19 | 无 Web UI | 全仓库（空命中） |
| E-20 | 子进程解析隔离 | `core/safe_process.py:112-114` |
