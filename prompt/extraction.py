"""LLM 提示词模板（独立成文件，便于 HR 阅读与迭代）。

借鉴 ``google/langextract`` 的「给定 Schema + 保留原文出处」思路：
每个抽取提示都要求模型返回 ``source_quotes``，把结论锚回原文，
既方便人工复核，也让抽取结果可追溯。
"""

from __future__ import annotations

from datetime import datetime

from schema.extraction import ExtractedCandidate
from schema.extraction import InterviewScheduleSchema, CandidateReplySchema


def _schema_hint(model: type) -> str:
    """把 Pydantic 模型的 JSON Schema 压成紧凑提示。"""
    import json

    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False)


# --------------------------------------------------------------------- 简历抽取

RESUME_SYSTEM = (
    "你是招聘系统的简历结构化引擎。只做信息抽取，不做评价、不做筛选建议。"
    "严格输出一个 JSON 对象，不要输出任何解释文字或 Markdown 围栏。"
)


def build_resume_prompt(resume_text: str, positions: list[tuple[str, str]]) -> str:
    position_lines = "\n".join(f"- {code}: {title}" for code, title in positions) or "- （暂无在招职位）"
    return f"""请从下面的简历文本中抽取结构化字段。

在招职位（如简历中写明应聘岗位，请把岗位名填到 position_hint，不要自己编造岗位代码）：
{position_lines}

字段要求：
- name/email/phone：原样抽取，缺失留空字符串
- education_level：只能是 大专 / 本科 / 硕士 / 博士 / 高中 之一，无法判断留空
- school：毕业院校全称
- major：专业全称
- years_experience：数字（年），无则 0
- skills：技能关键词数组（保留原文表述）
- project_highlights：项目亮点句数组，最多 6 条，必须是原文句子
- summary：不超过 100 字的中文摘要
- confidence：0-1，表示你对整体抽取准确度的判断
- source_quotes：数组，每项 {{"field": 字段名, "quote": 支撑该字段的原文片段}}

JSON Schema 参考：
{_schema_hint(ExtractedCandidate)}

简历文本：
---
{resume_text[:8000]}
---
"""


# --------------------------------------------------------------------- 面试安排

INTERVIEW_SYSTEM = (
    "你是 HR 招聘机器人的信息抽取引擎。你的任务是从部门主管的消息中抽取"
    "「要面试哪些人」和「面试在什么时间」。只输出 JSON 对象，不要解释。"
)

# Few-shot 样例（§4-A.2 量化调优，仅供 LLM 路径理解格式）：
# 只做示例，不改动既有抽取要求与 JSON Schema；样例姓名全部来自名单、时间全部可解析，
# 专门用于强化「姓名接地 + 中文时间」的正确范式。真实接地由 service 层强制，不依赖本样例。
INTERVIEW_FEW_SHOT = """\
示例（仅用于理解格式；姓名必须来自上面的名单，时间必须可解析，禁止把「12 份」「3 人」当姓名）：

输入：下周三下午两点张明和李四来面试
输出：{"candidate_names": ["张明", "李四"], "interview_time": "2026-11-11T14:00:00+08:00", "location": "", "position_title": "", "notes": "", "confidence": 0.95, "source_quotes": [{"field": "candidate_names", "quote": "张明和李四"}, {"field": "interview_time", "quote": "下周三下午两点"}]}

输入：这 12 份我只留了 3 份，请通知刘洋、陈静、周涛 3 人于下周二上午 10 点到公司 9 楼会议室面试
输出：{"candidate_names": ["刘洋", "陈静", "周涛"], "interview_time": "2026-11-10T10:00:00+08:00", "location": "9 楼会议室", "position_title": "", "notes": "", "confidence": 0.9, "source_quotes": [{"field": "candidate_names", "quote": "刘洋、陈静、周涛"}, {"field": "interview_time", "quote": "下周二上午 10 点"}]}

输入：请通知王小明，下周来面试（具体时间我后面再定）
输出：{"candidate_names": ["王小明"], "interview_time": null, "location": "", "position_title": "", "notes": "时间未定", "confidence": 0.6, "source_quotes": [{"field": "candidate_names", "quote": "王小明"}]}
（含糊无具体时间时 interview_time 必须填 null，不要编造时间）
"""


def build_interview_prompt(
    message_text: str,
    *,
    candidates: list[tuple[str, str]],
    now_local: datetime,
    position_title: str = "",
    location_default: str = "",
    source_channel: str = "飞书",
) -> str:
    roster = "\n".join(f"- {name}（{code}）" for name, code in candidates) or "- （当前没有待反馈的候选人）"
    return f"""当前时间：{now_local.strftime('%Y-%m-%d %H:%M')}（{now_local.tzname() or 'Asia/Shanghai'}，星期{'一二三四五六日'[now_local.weekday()]}）
岗位：{position_title or '（未指定）'}
默认面试地点：{location_default or '（未配置，写“地点待定”）'}

主管消息来源：{source_channel}
主管原文：
---
{message_text}
---

该岗位本轮已发给主管、等待反馈的候选人名单（**只能从这份名单里选人**，不要凭空造名字）：
{roster}

抽取要求：
1. candidate_names：只填名单中出现的姓名；主管说“全部”“都通知”时填名单里的全部人
2. interview_time：ISO 8601 带时区（如 2026-11-11T14:00:00+08:00）。中文时间换算规则：
   - “下周三”指下一个自然周的周三；“本周三/周三”若已过去则视为下周三
   - “下午 2 点”= 14:00，“晚上 7 点”= 19:00，“上午 10 点”= 10:00
   - 只给日期没给时间时按 10:00 处理；完全没提时间则填 null
3. location：主管指定的地点；没指定就填默认地点；都没有填空字符串
4. confidence：0-1
5. source_quotes：每项 {{"field", "quote"}}，quote 必须是原文片段

JSON Schema 参考：
{_schema_hint(InterviewScheduleSchema)}

{INTERVIEW_FEW_SHOT}
"""


# --------------------------------------------------------------------- 候选人回信

REPLY_SYSTEM = (
    "你是 HR 招聘机器人的邮件理解引擎。判断候选人对面试邀约的意向，"
    "并抽取其期望的新时间。只输出 JSON 对象，不要解释。"
)

# Few-shot 样例（§4-A.2 量化调优）：覆盖「改期 / 放弃 / 确认」三类意图，
# 用于强化意图识别与「改到 X」之后的时间抽取；不改动既有抽取要求与 Schema。
REPLY_FEW_SHOT = """\
示例（仅用于理解格式）：

输入：您好，我下周二临时有会，能否改到周四下午 3 点？
输出：{"intent": "reschedule", "proposed_time": "2026-11-05T15:00:00+08:00", "reason": "下周二有会", "confidence": 0.9, "source_quotes": [{"field": "intent", "quote": "改到周四下午 3 点"}, {"field": "proposed_time", "quote": "改到周四下午 3 点"}]}

输入：不好意思，我已经找到工作了，这次就不参加了。
输出：{"intent": "decline", "proposed_time": null, "reason": "已找到工作", "confidence": 0.95, "source_quotes": [{"field": "intent", "quote": "找到工作"}]}

输入：收到，我会准时参加面试，谢谢！
输出：{"intent": "confirm", "proposed_time": null, "reason": "", "confidence": 0.95, "source_quotes": []}
"""


def build_candidate_reply_prompt(
    body_text: str,
    *,
    candidate_name: str,
    position_title: str,
    current_interview_time: str,
    now_local: datetime,
) -> str:
    return f"""当前时间：{now_local.strftime('%Y-%m-%d %H:%M')}（星期{'一二三四五六日'[now_local.weekday()]}）
候选人：{candidate_name}
岗位：{position_title}
原定面试时间：{current_interview_time or '（未记录）'}

候选人回信正文：
---
{body_text[:4000]}
---

抽取要求：
- intent：confirm（确认参加）/ reschedule（想改时间）/ decline（放弃）/ other
- proposed_time：候选人希望的新时间，ISO 8601 带时区；没提则 null
- reason：候选人给出的原因，不超过 60 字
- confidence：0-1
- source_quotes：每项 {{"field", "quote"}}，quote 必须是原文片段

JSON Schema 参考：
{_schema_hint(CandidateReplySchema)}

{REPLY_FEW_SHOT}
"""


# --------------------------------------------------------------------- 语义匹配

SEMANTIC_SYSTEM = (
    "你是资深技术招聘官，负责判断简历与岗位 JD 的匹配度。"
    "只做匹配度评估：不编造简历里没有的经历，不因为岗位描述里出现某个词就假定候选人会。"
    "严格输出一个 JSON 对象，不要输出任何解释文字或 Markdown 围栏。"
)


def build_semantic_prompt(
    *,
    company_name: str,
    position_title: str,
    position_description: str,
    resume_summary: str,
    skills: list[str],
    project_highlights: list[str],
    education_level: str = "",
    school: str = "",
    major: str = "",
    years_experience: float = 0.0,
) -> str:
    """与 ``prompt/screening.md`` 第 4 节保持一致的语义打分提示。

    除了摘要/技能/项目，还把学历、院校、专业、年限一起给模型——否则它无法判断
    「JD 要求硕士」这类硬门槛的语义含义，只能靠猜。
    """
    return f"""你是{company_name}的招聘助理，请判断候选人简历与岗位的匹配程度。

岗位：{position_title}
岗位描述：{position_description}

候选人背景：
- 学历：{education_level or '未提供'}
- 院校：{school or '未提供'}
- 专业：{major or '未提供'}
- 工作年限：{years_experience:g} 年

候选人简历摘要：
{resume_summary or '（未提供摘要）'}

技能：{'、'.join(skills) or '（未提取）'}
项目：{'；'.join(project_highlights) or '（未提取）'}

评分口径（只看「能力是否对得上 JD」，不看简历写得好不好）：
- 90-100：核心职责与关键技术栈高度对口，可直接进入面试
- 70-89：主要能力对口，存在 1-2 项可培养的差距
- 50-69：部分对口，需要较长上手期
- 30-49：仅边缘相关，主要靠转岗学习
- 0-29：明显不对口

要求：
1. 同义表述要算匹配，例如 JD 要「微服务」而简历写「Spring Cloud / 服务拆分」，应判为匹配；
2. 只有简历里能找到依据才加分，找不到就按「不具备」处理，禁止脑补；
3. reason 不超过 60 字，必须点出关键依据（可引用简历原句片段）。

请只输出 JSON：{{"semantic_score": 0-100 的整数, "reason": "不超过 60 字的中文理由"}}
"""


# --------------------------------------------------------------------- 深度解析
# 2026-11-13 新增：语义分只给一个总分，主管拿到简历包还是得自己看完 12 份才知道
# 谁值得面。这一层在语义分之上再要四样东西：分维度评分、亮点、风险、面试追问点，
# 全部要求「有原文依据」，没有依据就留空——宁可少给，不许脑补。

DEEP_ANALYSIS_SYSTEM = (
    "你是资深技术招聘官，负责对简历做深度评估。"
    "所有结论必须有简历原文依据；找不到依据的维度一律给 0 并在 basis 里写“简历未体现”。"
    "只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏。"
)


def build_deep_prompt(
    *,
    company_name: str,
    position_title: str,
    position_description: str,
    resume_text: str,
    skills: list[str],
    project_highlights: list[str],
    education_level: str = "",
    school: str = "",
    major: str = "",
    years_experience: float = 0.0,
) -> str:
    return f"""请对下面这份简历做「岗位匹配深度评估」，输出可供 HR 直接决策的结构化结论。

岗位：{position_title}
岗位描述：{position_description}
公司：{company_name}

候选人档案：
- 学历：{education_level or '未提供'}｜院校：{school or '未提供'}｜专业：{major or '未提供'}
- 工作年限：{years_experience:g} 年
- 技能：{'、'.join(skills) or '（未提取）'}
- 项目：{'；'.join(project_highlights) or '（未提取）'}

简历正文（截断）：
---
{resume_text[:6000]}
---

评估要求：
1. semantic_score：0-100 的岗位匹配总分，口径同「只看能力是否对得上 JD」；
2. dimensions：四个维度各 0-100，缺依据给 0
   - skill_match：技术栈/工具与 JD 的重合度
   - experience_depth：项目复杂度、责任范围、年限含金量
   - delivery_ability：是否有上线、量化结果、跨团队协作证据
   - growth_potential：学习曲线、技术广度、可培养性
3. strengths：最多 3 条，每条不超过 30 字，必须能在简历里找到依据
4. risks：最多 3 条，例如「无高并发场景」「跳槽频繁」「只做过外包项目」；没有就空数组
5. interview_focus：最多 3 条，面试时该追问什么来验证风险
6. reason：不超过 60 字的总评，点出关键依据

请只输出 JSON：
{{"semantic_score": 0, "dimensions": {{"skill_match": 0, "experience_depth": 0, "delivery_ability": 0, "growth_potential": 0}}, "strengths": [], "risks": [], "interview_focus": [], "reason": ""}}
注意：上面的 0 与空数组只是字段占位，**必须替换成这份简历的真实评分与真实结论**；
原样照抄占位符会被判为无效输出，直接降级成「只按规则分」。
"""
