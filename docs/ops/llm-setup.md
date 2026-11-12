# LLM 接入配置（litellm 统一网关 + 自动降级）

> 对应改动：`AGENT.md` §4-A.6(b)。本文说明如何配置 Key、如何切换模型、以及降级顺序。
> 实现文件：`client/llm.py`（`LLMClient.complete_json` 对外签名不变，调用方零改动）。

---

## 1. 原理一句话

本项目的 LLM 调用统一走 **litellm**（`litellm.acompletion`），用一个入口对接多家供应商。
模型名自带供应商前缀（`gpt-4o-mini` / `claude-3-5-sonnet-...` / `gemini/...` 等），litellm 据此
路由到对应厂商并注入相应凭证。**主模型失败后，按配置的 fallback 列表依次降级**，全部失败
抛 `LLMUnavailable`，由调用方（筛选 / 抽取）落到既有的确定性规则抽取。

> 关键红线：**降级只发生在「调用失败 / 超时 / 无 key」**，绝不因为"LLM 返回了结果但抽取
> 不理想"而降级——那会绕过 I-05 姓名接地。抽取质量不理想不是降级理由。

---

## 2. 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_MODE` | `auto` | `auto`=有 key 才调；`live`=强制调；`off`=只用规则抽取（离线闭环） |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 自建网关 / 代理时改这里（兼容 OpenAI 协议） |
| `OPENAI_API_KEY` | （空） | API Key；用其他供应商时按 litellm 约定设置对应 key 变量 |
| `LLM_MODEL` | `gpt-4o-mini` | **主模型**，模型名前缀决定供应商 |
| `LLM_FALLBACK_MODELS` | （空） | **降级候选模型**，逗号分隔，可空；按顺序尝试 |
| `LLM_TIMEOUT_SECONDS` | `45` | 单次调用超时（秒） |

### 2.1 配置 Key

- **OpenAI**：`OPENAI_API_KEY=sk-...`（litellm 默认读取）。
- **其他供应商**：litellm 约定为 `ANTHROPIC_API_KEY`、`GEMINI_API_KEY`、`AZURE_API_KEY`
  等，各自对应模型前缀（`claude-*` / `gemini/*` / `azure/...`）。**无需改代码**，改模型名
  + 设置对应 key 即可。

> ⚠️ **本项目只读 `OPENAI_API_KEY` 这一个字段**（`config/settings.py` 的 `extra="ignore"`，
> 没有 `deepseek_api_key` / `anthropic_api_key` 字段），而 `client/llm.py` 是显式把
> `api_base` + `api_key` 传给 litellm 的——所以无论用哪家供应商，key 都写 `OPENAI_API_KEY`，
> 供应商只由 `LLM_MODEL` 前缀 + `OPENAI_BASE_URL` 决定。这一点在 §6 有实测对照。

### 2.2 切换模型

只改两个变量即可，代码零改动（**`OPENAI_BASE_URL` 必须跟着供应商一起改**）：

```bash
# DeepSeek（已真机验证）
LLM_MODEL=deepseek/deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com/v1

# OpenAI 官方
LLM_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1

# 降级候选（与主模型共用同一个 api_base，只能填同一供应商的模型）
LLM_FALLBACK_MODELS=deepseek/deepseek-reasoner
```

### 2.3 离线（无 key / 规则抽取主路径）

```bash
LLM_MODE=off        # 或：LLM_MODE=auto 且不设 OPENAI_API_KEY
```

此模式下 `LLMClient.enabled == False`，`complete_json` 直接抛 `LLMUnavailable`，
`service/screening.py` / `service/extraction.py` 捕获后落到**确定性规则抽取**，
`scripts/demo.py` 完整闭环照常跑通。

---

## 3. 降级顺序

```
LLM_MODE 判定
   ├─ off / 无 key  ──────────────► LLMUnavailable ──► 规则抽取（主路径）
   └─ 启用
        ├─ 1) 主模型 LLM_MODEL
        │      ├─ 成功 ───────────► 返回 JSON
        │      └─ 失败（网络/超时/结构/非法 JSON/底层异常）
        │                          ▼ 日志 llm.call_failed
        ├─ 2) LLM_FALLBACK_MODELS[0]
        │      └─ 失败 ───────────► 继续
        ├─ 3) LLM_FALLBACK_MODELS[1] ... 依序尝试
        └─ 全部失败 ───────────────► LLMUnavailable ──► 规则抽取
```

**日志关键点**（可在 `var/logs` / 控制台检索）：

- `llm.call_failed`（`model=...`, `error=...`）：某个模型调用失败；
- `llm.all_fallback_failed`（`models=[...]`）：主模型与全部 fallback 均失败，准备回落规则。

---

## 4. 验证

### 4.1 有 key（走 litellm）

```bash
# .env 里：LLM_MODE=auto / OPENAI_API_KEY=<真实key> / LLM_MODEL=gpt-4o-mini
./.venv/Scripts/python.exe scripts/eval_extraction.py
```

预期：不再打印 `抽取路径：rule` 字样；有真实 LLM 返回，且姓名/意图准确率与规则路径可比。
（注：`eval_extraction.py` 当前实现显式 `llm=None` 走规则路径；如需验证 litellm 真机调用，
用下面 4.3 的 fallback 场景，或临时把 `LLM_MODE=live` 跑一次 `scripts/demo.py`。）

### 4.2 无 key（规则抽取主路径，验收硬指标）

```bash
./.venv/Scripts/python.exe scripts/demo.py
```

预期：`LLM_MODE=off`（或无 key）时完整闭环跑通，抽取全部走 `method=rule`，
输出形如 `机器人判定动作：interview_scheduled` 等，**不因缺 key 而中断**。

### 4.3 模拟主模型失败 → 触发 fallback

```bash
# 故意写错主模型，并配置一个可用的 fallback
export LLM_MODE=live
export OPENAI_API_KEY=<真实key>
export LLM_MODEL=not-a-real-model
export LLM_FALLBACK_MODELS=gpt-4o-mini
./.venv/Scripts/python.exe scripts/demo.py
```

预期：日志中先出现 `llm.call_failed`（`model=not-a-real-model`），随后成功切到
`gpt-4o-mini`；若把 fallback 也写错，则出现 `llm.all_fallback_failed` 并回落规则抽取。

---

## 5. 边界（本项目主动不做）

- **不**新增除 `litellm` 以外的任何依赖；
- **不**改 `schema/extraction.py`（`source_quotes` / `method` 字段保留，litellm 只换运输层）；
- **不**新增 LLM 相关数据库表 / 缓存层 / 重试队列；
- **不**把"抽取不理想"当降级理由（I-05 姓名接地不可绕过）。

---

## 6. 真机联调记录（2026-11-05，已通过）

> 本节是「真机跑过」的证据，不是预期。环境：Windows / Python 3.13.14 / litellm 1.108.0 /
> 供应商 **DeepSeek 官方**（OpenAI 兼容协议，国内直连）。key 只存在本机 `.env`，不在仓库里。

联调用的 `.env`（关键三行）：

```bash
LLM_MODE=live
LLM_MODEL=deepseek/deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-****（DeepSeek 控制台创建的 35 位 key）
```

联调方式：直接调用 `client.llm.LLMClient.complete_json(system=..., user='返回 {"ok": true}')`，
走的是**与业务完全相同的代码路径**（screening / extraction 也走这个方法）。

| 组合 | 配置 | 结果 | 耗时 |
|---|---|---|---|
| A | `LLM_MODEL=deepseek/deepseek-chat` + `OPENAI_BASE_URL=https://api.openai.com/v1`（即只换模型、没换 base） | ✗ `LLMUnavailable`（litellm 侧 `InternalServerError` 502） | 16.5s（含重试） |
| B | 同上，但 `OPENAI_BASE_URL=https://api.deepseek.com/v1` | ✓ 返回合法 JSON `{'ok': True, 'model_echo': 'ping'}` | **0.6 ~ 0.9s** |
| C | 主模型故意写成 `not-a-real-model`，`LLM_FALLBACK_MODELS=deepseek/deepseek-chat` | ✓ 自动降级成功：日志先 `llm.call_failed(model=not-a-real-model)`，随后切到 fallback 拿回 JSON | 1.0s |

### 6.1 结论

1. **真机链路已通**：`LLMClient.complete_json` → litellm → DeepSeek → `strip_json_fence` → `json.loads`
   全链路返回 dict，业务侧（筛选/抽取）无需任何改动。
2. **`api_base` 必须与模型供应商一致**。`client/llm.py` 是无条件
   `api_base=self.settings.openai_base_url`，用 DeepSeek 模型却指向 OpenAI 官方会 502/401
   （组合 A 的实测结果）。这是最容易踩的坑，`.env.example` 已把两行写在一起并加注释。
3. **key 只能写 `OPENAI_API_KEY`**。`.env` 里写 `DEEPSEEK_API_KEY` 不会生效：
   `Settings` 没有该字段（`extra="ignore"` 直接忽略），而 litellm 读的是 `os.environ`，
   `.env` 只被 pydantic-settings 灌进 `Settings`、不会进环境变量。
4. **降级链在真实失败下确实生效**（组合 C），且失败原因落在 `llm.call_failed` / 
   `llm.all_fallback_failed` 两个日志点上，运维可检索。
5. `litellm` 是**延迟导入**（`client/llm.py::_call_litellm` 内 `import litellm`），
   因此 `LLM_MODE=off` / 无 key 的离线闭环**不需要**这个包也能跑完 `demo.py`。

### 6.2 本次没覆盖的

- **OpenAI 官方模型**：本机没有 OpenAI 额度，组合 A/B 只验证了 DeepSeek。切 OpenAI 只需把
  `LLM_MODEL` + `OPENAI_BASE_URL` + key 三处一起换；
- **抽取质量**：真机联调只证明「运输层通、JSON 可解析」。筛选/抽取准确率仍要人工抽样复核
  （`source_quotes` 字段就是为此保留的，见 §5 边界）；
- **多供应商混合 fallback**：`fallback` 与主模型共用同一个 `api_base`，跨供应商降级当前做不到
  （要改 `client/llm.py` 的传参方式，属未授权改动）。
