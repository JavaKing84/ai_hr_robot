# 09 · 开放问题与待确认清单（OPEN QUESTIONS）

- 生成时间：2026-11-14T01:05+08:00
- 分支 / commit：`develop` / `bf2cc81fb7b18aafc45c2e47ae844def216e4b8e`（2026-11-13 20:58:20 +0800）
- 依据：本次对全仓库 `.py`（105 个 / 16,818 行，其中业务代码 67 个 / 11,280 行）静态通读 + 1 次真实 pytest 运行（190 passed）+ 1 次真实 PostgreSQL 只读核对
- 编号规则：`Q-*` 全局唯一；与 `00/05/07` 中的 `D-*`（文档差异）、`R-*`（部署风险）、`G-*`（测试缺口）、`K-*`（代码风险）互相引用，不重复定义

> **本文件只列"不确定"的东西，不下结论。** 每条都写清：为什么不确定 / 影响什么 / 如何验证。
> 凡本文件已给出现象与验证命令的条目，说明**源码事实部分已经确认**，不确定的是"意图"或"是否已在真实环境成立"。

---

## 1. 文档与源码不一致（已核实，7 条）

这 7 条不是"猜"，是本次逐行核对出来的硬差异。**需要决定的是：改文档，还是改代码。**

| ID | 不一致点 | 源码事实（相对路径:行） | 判据类型 | 影响 | 处置选项 |
|---|---|---|---|---|---|
| Q-1 | `Dockerfile` 启动的 `main.py` 不存在 | `Dockerfile:27`（全文 27 行，CMD 是最后一行）`CMD ["python","main.py"]`；`core/main.py:1` 注释"原根目录 main.py"；仓库根 `ls *.py` 为空 | 直接证实 | `docker compose up --build` 起不来（R-3 / K-11） | ① 改 CMD 为 `uvicorn factory.app:create_app --factory`；② 补一个根 `main.py` 入口壳 |
| Q-2 | 状态枚举文档称 10 个，实际 11 个 | `model/candidate.py:20-61`（含 `interview_pending_confirm`，`CandidateStatus.ALL` 共 11 项）；文档侧见 `AGENT.md:188`、`docs/design/architecture.md:4,177,209`、`docs/DECISIONS.md:13,40`、`service/ingestion.py:376` 注释 | 直接证实 | 新人按文档写状态机必漏一态；该态在 `service/notification.py:482`、`service/feedback.py:686` 真实使用 | 建议改文档为 11（代码是对的） |
| Q-3 | `is_duplicate` / `duplicate_of_id` 两列从不写入 | `model/candidate.py:4-5` 注释称重复投递会标出；实际 `grep -rn "duplicate_of_id"` 仅 3 处（model 定义 + 迁移 + 无赋值）；重复投递在 `service/ingestion.py:240-245` **直接返回首条记录 + 一条事件，不新建行** | 直接证实 | 任何"按 `is_duplicate` 统计重复率"的查询恒为 0（K-3） | 需产品确认：保留双列做未来扩展，还是删列（删列要走迁移） |
| Q-4 | 文档称"未引入 Alembic"，实际两套建表路径并存 | `AGENT.md:205`、`docs/DECISIONS.md:40` 称只用 `create_all`；实际存在 `alembic.ini` + `migrations/versions/0001_initial.py`，且**运行时仍走 `init_models()`（create_all）**，见 `factory/lifespan.py:90` | 直接证实 | 加字段后老库缺列 → 启动即 `UndefinedColumn`（K-6 / G-4） | 需定策略：迁移为准（启动不建表）还是 create_all 为准（废 Alembic） |
| Q-5 | README 指向的文档路径已失效 | `README.md:7` 指向根目录 `API.md`、`DECISIONS.md`；实际在 `docs/API.md`、`docs/DECISIONS.md`，根目录只有 `README.md`、`AGENT.md` | 直接证实 | 读者点不到；新人以为文档缺失 | 改 README 两处链接即可 |
| Q-6 | 用例数两份文档不一致 | `docs/quality/testing-report.md:5,94` 称 **154**；`README.md:441` 称 **190**；本次实测收集 **190** 且全绿（`07_TESTS_AND_GAPS.md` §1.2） | 运行证实 | 质量报告失真 36 例 | 以实测 190 为准，回填测试报告 |
| Q-7 | 测试报告写的项目根目录与本机不符 | `docs/quality/testing-report.md:73` 写 `E:\project\hr-ai-robot`；本机为 `D:\!pythonproject\hr_ai_robot` | 运行证实 | 按文档路径复跑会失败 | 报告里改成相对路径，不写死盘符 |

---

## 2. 需求歧义（源码无法判定"这是不是想要的"）

### Q-8 发送结果不影响状态推进 —— 是"尽力发送"还是漏判？
- **现象（直接证实）**：`service/notification.py:175-192`，`dispatch` 无论 `feishu_text.ok` 真假都置 `dispatched`；`:422-426`，通知失败也置 `interview_notified`。
- **为什么不确定**：代码没有任何注释或文档说明这是"故意乐观推进"，也没有补偿任务。无法区分"设计如此"与"忘了判断"。
- **影响**：飞书/邮件挂掉时，主管看不到简历包，但系统认为已送达，下一轮不会重发；只能人工从 `messages.status=failed` 捞（K-1）。
- **如何验证**：在 `docker`/本机把 `FEISHU_MODE=mock` 且制造发送异常，跑 `scripts/run_cycle_once.py`，观察 `messages.status` 是否仍变 `dispatched`。

### Q-9 重复投递到底要不要落"重复关系"行？
- **现象**：`model/candidate.py:4-5` 注释说要标关系，`service/ingestion.py:240-245` 实际不建行、只记一条事件；`IngestOutcome.is_duplicate` 是纯内存字段。
- **为什么不确定**：注释与实现相反，且两处都能自圆其说（"不建行"省存储、语义干净；"建行"可追溯投递历史）。
- **影响**：影响"同一人投多个岗位"的统计口径；也影响未来做投递历史页。
- **如何验证**：拿两份同一邮箱的简历分别投两个岗位，查 `candidates` 行数与 `candidate_events` 条数，确认是否符合产品预期。

### Q-10 面试通知人工确认闸门，生产该开还是该关？
- **现象（直接证实）**：`config/settings.py:66-69`，默认 `True`，注释写得很清楚：`True = 只挂起、不发送，必须由 HR 人工确认后才投递；False = 主管给出名单后直接发送（生产环境确认过再用）`。
- **为什么不确定**：**方向是给了的**（"生产确认过再用"= 验证通过后置 `False`），不确定的是两个执行层问题：① "确认到什么程度算确认"没有验收标准；② 闸门挂起后**无超时、无提醒、无兜底**（K-8），开启期间 HR 漏回一次，该候选人就永久停在 `pending_approval`。
- **影响**：开 = 安全但可能卡死流程；关 = 全自动外发、无人复核。两种都不会报错，只会静默。
- **如何验证**：`grep -rn "pending_approval" service/ repository/` —— 当前只看到状态置位与查询，**没有定时扫描/超时推进任务**；再故意不回复一次，观察次日 cron 是否重发确认请求。

### Q-11 语义分影子的退出条件是什么？
- **现象（直接证实）**：`config/settings.py:78-80` 注释给了原理——`True = 照常调用大模型打分并落库留痕，但「通过与否」仍按规则分判定`，并说明"阈值 55/60 是按纯规则分布定的，语义分一上来就参与判定，通过名单会整体平移，无法判断是改好了还是改坏了"；`service/screening.py:463-465` 影子时 `applied=False` 且 note 追加"影子模式：语义分仅记录，未参与判定"。
- **为什么不确定**：注释完整解释了**为什么影子**，但没有任何"退出条件"（达到多少样本量、多少与规则分一致率、AUC 多少就切正式）。
- **影响**：默认配置下语义能力等于**未上线**；若误以为它已在影响筛选结果，会高估系统能力。
- **如何验证**：跑 `python scripts/ab_semantic.py`（该脚本注释写明"在把 SEMANTIC_SHADOW 改成 false 之前，先看清语义分会把谁救回来"），按它输出的差异样本决定是否切正式。

### Q-12 `INBOUND_POLL_SECONDS=0` 是"每天只收一轮"还是漏配？
- **现象**：本机 `.env` 为 `0`；调度侧每日 cron 触发一次 `run_daily_cycle`（`factory/lifespan.py:48-82`）。
- **为什么不确定**：`0` 在语义上既可读作"关闭轮询（只靠 cron）"，也可能被误读成"立刻轮询"。
- **影响**：候选人回信/主管邮件最多延迟到次日 09:00 处理（R-7）；飞书反馈是实时的，两者体验不一致。
- **如何验证**：改 `INBOUND_POLL_SECONDS=300` 启动，观察 `var/` 日志里收信任务是否按 5 分钟触发。

---

## 3. 源码中找不到实现（能力缺口，不是 bug 报告）

| ID | 缺什么 | 证据（grep 结果为空 / 只有定义） | 影响 |
|---|---|---|---|
| Q-13 | **任何 API 鉴权** | `grep -rnE "HTTPBearer|Security\(|Depends\(.*(auth|verify|api_key)" api/ core/deps.py` → **0 条**；`factory/middleware.py:16-18` 只挂了 `AccessLogMiddleware` + `RequestIdMiddleware`；`api/v1/router.py:7` `APIRouter()` 无 `dependencies` | 能访问端口的人都能调 `/api/v1/recruitment/*`、`/api/v1/feishu/*`，包括上传简历、触发 run-cycle |
| Q-14 | 限流 / 防刷 | `grep -rniE "rate.?limit|slowapi"` → 0 条 | 回调接口可被打爆；LLM 调用可被刷出账单 |
| Q-15 | 飞书回调**既不验签、token 校验又可绕过** | `client/feishu.py:412-415`：`if expected and token and token != expected` —— `expected`（`FEISHU_VERIFICATION_TOKEN`）为空时整个校验被跳过；全仓库无 `X-Lark-Signature`/HMAC 校验（`grep -niE "signature\|hmac" client/feishu.py` 空命中）；`:409-410` 虽有 `AESCipher` 解密路径，但 `FEISHU_ENCRYPT_KEY` 亦为空 → 带 `encrypt` 字段的请求会直接抛 `ConfigurationError`。本机 `.env` 两键均"存在但为空"（仅查键状态，未读值） | 伪造**明文** `im.message.receive_v1` 事件即可让机器人发面试邮件（被 K-8 确认闸门部分抵消）（R-1） |
| Q-16 | 统一重试 / 死信队列 | 无 retry 装饰器、无退避策略；各处仅 `try/except` 转成自定义异常 | 瞬时故障只能等下一轮 cron（最长 24h） |
| Q-17 | 发送失败告警 | 无邮件/飞书告警通道；失败只写日志与 `status=failed` | 故障靠人工翻库发现（K-1） |
| Q-18 | `pending_approval` 超时提醒 | `grep -rn "pending_approval"` 仅见状态置位与查询，无定时扫描 | 挂住的面试通知永不推进（Q-10） |
| Q-19 | 简历包分发幂等键 | `dispatch_cycle` 无幂等键，仅靠查询已有 `messages` 记录 | 多副本/重复触发会重复发主管（G-5） |
| Q-20 | `alembic upgrade head` 的可运行性验证 | 无迁移测试；本机 `hr_robot` 库**没有 `alembic_version` 表**（docker 只读核对） | 迁移文件是否真能跑通**未经验证**，只做过列定义比对（G-4 / K-6） |
| Q-21 | `service/agent.py`（460 行）的自动化测试 | `grep -rln "agent" tests/*.py` → 空；`llm_mode=off` 时 `plan` 恒 `None`，测试走不到该分支 | 2026-11-13 新增的最大模块只靠手工联调（G-1） |

---

## 4. 需要用户 / 老师 / 运维确认（决策项，我不擅自处置）

### Q-22 本机 `.venv` 已损坏，怎么修？（阻塞一切本地运行）
- **现象（运行证实）**：`python -V` = **3.14.7**，而 `.venv/Lib/site-packages` 里 `asyncpg` / `greenlet` / `pydantic_core` 的二进制是 `cp313-win_amd64.pyd`。`import asyncpg` → `No module named 'asyncpg.protocol.protocol'`；`import pydantic_core` → `cannot import name 'ArgsKwargs'`。
- **时间线**：00:51 同一 venv 还能跑完 190 个用例；00:57 起同一路径报 3.14.7。说明期间有外部动作换了解释器。
- **为什么不自己修**：重建 venv / 重装依赖属于**改环境**，超出"未经明确要求不改动"的边界。
- **影响**：应用与 pytest 当前都起不来（R-4）。
- **如何验证 / 修复方案（二选一，请指定）**：
  - A. 用 Python 3.13 重建：`py -3.13 -m venv .venv` → `pip install -r requirements.txt -r requirements-dev.txt`
  - B. 保留 3.14，全量重装：`python -m pip install --force-reinstall -r requirements.txt -r requirements-dev.txt`

### Q-23 Docker 链路要不要修？改哪个？
- 需确认：`Dockerfile:27` 改 `CMD`，还是补根 `main.py`（见 Q-1）。**改代码需你明确授权**。
- 附带确认：`docker-compose.yml:44-55` 的 `app` 服务**不挂 `var/` 卷**，容器重建会丢简历原件、zip、IMAP 水位（R-5）——要不要一起改。

### Q-24 生产数据库用哪个账号？
- 现状：本机 `.env` 的 `DATABASE_URL` 用 `postgres` **超级用户**（R-2）；compose 里写死 `hr:hr`（R-6）。
- 需确认：生产是否建独立最小权限账号、口令如何注入（secret 管理方案）。

### Q-25 生产的飞书配置是否已配齐？
- 需确认：`FEISHU_VERIFICATION_TOKEN` 是否非空（为空 = 回调验签失效，Q-15）；应用是否已开 `im:resource:upload` 权限（缺权限时群推送失败只在 live 暴露，G-3）。
- 如何验证：`python scripts/probe_llm.py` 只能验 LLM；飞书需实跑一次群推送。

### Q-26 生产 `INTERVIEW_NOTICE_REQUIRE_CONFIRM` 取什么值？
- 见 Q-10。这是**业务决策**，不是技术问题。

### Q-27 真实邮箱 / 飞书应用的归属与配额
- 需确认：163 邮箱是否开了客户端授权码、单日发信配额、IMAP 是否可长连；飞书自建应用的可用范围与回调地址是否已备案。
- 影响：配额打满会导致面试通知静默失败（叠加 K-1 更隐蔽）。

---

## 5. 需要真实环境验证（本机条件不具备 / 会外发，未执行）

| ID | 待验证项 | 为什么没验证 | 建议验证方式 |
|---|---|---|---|
| Q-28 | 真实 SMTP 发信 + IMAP 收信全链路 | 会真实外发邮件、需真实授权码；本次禁止副作用 | 用测试邮箱对发；`scripts/check_mail_login.py` 先验登录，再 `scripts/e2e_four_chains.py` |
| Q-29 | 真实飞书 API（群推送、图片上传、事件回调） | 需真实应用凭据；测试全是 mock（G-3） | 设 `FEISHU_MODE=live`，跑 `scripts/run_cycle_once.py`，看群消息与 `99991672` 类错误 |
| Q-30 | `docker compose up --build` | 需 Docker Desktop 运行 + 拉基础镜像；且 `CMD` 指向不存在的文件（Q-1） | 修完 CMD 后 `docker compose up --build`，看 app 是否 healthy |
| Q-31 | `alembic upgrade head` 在空库 | 未跑迁移（G-4 / Q-20） | 新建空库 → `alembic upgrade head` → 对比 `information_schema.columns` 与 `model/` 定义 |
| Q-32 | LLM 降级链真实切换 | 需真实 key 与网络；`LLM_PROVIDERS` 是 JSON 数组，链路只在异常时切换 | 配 2 个 provider，第二个故意填错 key，看是否静默降级且日志有记录 |
| Q-33 | 双进程并发（多副本） | 无并发测试（G-5）；`max_instances=1` 只约束**单进程内**的同一 job | 起两个 uvicorn 实例同连一库，观察是否重复发简历包（Q-19） |
| Q-34 | 大附件 / 超大简历边界 | 无边界测试；`client/mailbox.py:367-386` 无大小上限，解析有 60s 超时但 IMAP fetch 没有（G-6） | 投一封 20MB PDF，观察整轮耗时与超时行为 |

---

## 6. 优先级建议（如果只能处理 5 件事）

1. **Q-22 修 venv** —— 否则本机什么都动不了。
2. **Q-1 / Q-23 修 Docker CMD** —— 否则"容器化部署"这条路径等于不存在。
3. **Q-13 / Q-15 鉴权与验签** —— 当前任何能访问端口的人都能触发外发邮件。
4. **Q-8 发送状态** —— 影响"以为发了其实没发"，是业务事故型缺陷。
5. **Q-4 / Q-20 建表策略** —— 决定后续每次加字段会不会把老库搞挂。

---

## 7. 扫描完整性与边界声明（避免误读为"全仓库已验证"）

- **已逐行读过**：`api/`、`client/`、`config/`、`core/`、`factory/`、`model/`、`repository/`、`schema/`、`service/` 的全部 `.py`；`migrations/versions/0001_initial.py`、`migrations/env.py`；`docs/design/`、`docs/quality/`、`AGENT.md`、`README.md`；配置与部署文件。
- **只做了目录级/索引级确认**：`prompt/`（`agent.py`、`extraction.py`、`screening.md` 未逐字读）、`scripts/`（16 个脚本只按名称与用途定位，未逐个通读）、`docs/guide/`、`docs/ops/`、`docs/archive/`。
- **明确排除**：`.venv/`、`.git/`、`.pytest_cache/`、`.idea/`、`var/`（运行时产物）、`tests/testdata/`、`.env` 的**取值**（只核对键名与开关状态，敏感值一律 `[REDACTED]`）。
- **未执行**：任何外发网络请求、任何代码修改、任何数据库迁移、任何依赖安装。
