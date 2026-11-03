# 简历筛选模板（SKILL）· ai-hr 飞书 HR 招聘机器人

> 这份文件就是产品文档里说的「筛选模板 / SKILL」。**HR 改这里 + 改 `config/positions.yaml`
> 里的条件，下一轮定时任务就按新规则跑**，不需要改代码。
>
> 执行顺序固定为：**硬性条件 → 加权打分 → （可选）语义匹配 → 阈值判定**。

---

## 1. 输入

每份简历先由 `client/resume_parser.py` 转成纯文本，再抽取为结构化字段：

| 字段 | 含义 | 示例 |
|---|---|---|
| `name` / `email` / `phone` | 身份与联系方式 | 张三 / zhangsan@example.com / 13800001111 |
| `education_level` | 学历（大专 / 本科 / 硕士 / 博士） | 本科 |
| `school` | 毕业院校 | 华北电力大学 |
| `major` | 专业 | 计算机科学与技术 |
| `years_experience` | 工作年限（数字） | 5 |
| `skills` | 技能关键词列表 | ["Python", "FastAPI", "PostgreSQL"] |
| `project_highlights` | 项目亮点句 | "主导订单服务重构，QPS 提升 3 倍" |
| `summary` | 一句话摘要 | — |

职位侧配置来自 `config/positions.yaml` 的 `positions[].screening`。

---

## 2. 硬性条件（不过即淘汰）

`hard_conditions[]` 逐条判定，**任意一条不满足，该候选人立即淘汰**，不参与排序，
状态置为 `screened_rejected`，淘汰原因写入 `hard_failed` 并进入事件留痕。

支持算子：

| 算子 | 语义 | 例子 |
|---|---|---|
| `gte` / `lte` | 大于等于 / 小于等于（数字或学历等级） | `years_experience gte 3`、`education_level gte 本科` |
| `eq` / `ne` | 相等 / 不等 | `major eq 计算机科学与技术` |
| `in` / `not_in` | 属于 / 不属于给定集合 | `school in [清华大学, 北京大学]` |
| `contains_any` / `contains_all` | 文本包含任意 / 全部关键词 | `major contains_any [计算机, 软件]` |
| `exists` | 字段非空 | `email exists true` |

学历按 **大专 < 本科 < 硕士 < 博士** 排序比较；`高中/中专` 视为低于大专。

---

## 3. 加权打分（0–100）

`weighted[]` 每条给出 0–1 的命中比例 `ratio`，按权重归一：

```
rule_score = Σ(ratio_i × weight_i) / Σ(weight_i) × 100
```

- `contains_any`：`命中关键词数 / 关键词总数`（命中得越多分越高）
- `gte` / `lte`：满足得 1，不满足得 0
- `in` / `not_in`：属于得 1，否则 0
- 命中明细（命中/未命中的关键词）写入 `score_detail`，供 HR 复核

**阈值**：`threshold` 由 HR 按岗位设定，`rule_score < threshold` 即为未通过。

---

## 4. 语义匹配（可选，`semantic_weight > 0` 时启用）

规则打分只能看关键词，看不出「做过同类系统」这类语义信息。开启语义匹配后，
把 JD 与简历摘要交给大模型打分（0–100），最终分：

```
final_score = (rule_score × (100 - semantic_weight) + semantic_score × semantic_weight) / 100
```

语义分只做加权，**不能推翻硬性条件**。

### 语义匹配提示词（发给模型的原文）

```
你是{{company_name}}的招聘助理，请判断候选人简历与岗位的匹配程度。

岗位：{{position_title}}
岗位描述：{{position_description}}

候选人简历摘要：
{{resume_summary}}

技能：{{skills}}
项目：{{project_highlights}}

请只输出 JSON：{"semantic_score": 0-100 的整数, "reason": "不超过 60 字的中文理由"}
评分口径：
- 90-100：经历高度对口，可直接面试
- 70-89：主要能力对口，有小幅差距
- 50-69：部分对口，需要培养
- 0-49：明显不对口
```

---

## 5. 输出

每个职位下输出两类集合：

1. `screened_passed`：硬性条件全过 **且** 最终分 ≥ 阈值 → 按分数从高到低排序，
   按部门打包发给主管（压缩包 + 摘要）。
2. `screened_rejected`：其余 → **不做任何后续动作**（不发感谢信、不进人才库，
   只保留状态与事件记录，产品文档第 5 节明确划掉）。

---

## 6. HR 修改指南

- 只想改门槛：改 `threshold`。
- 想加一条必须满足的条件：在 `hard_conditions` 里加一项，写清 `field / operator / value / label`。
- 想调整排序偏好：改 `weighted` 的 `weight`（相对值，不必凑满 100）。
- 改完保存 YAML 即可，**下一轮任务启动时会自动同步进数据库**（`PositionRepository.sync_from_config`）。
