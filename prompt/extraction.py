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
"""


# --------------------------------------------------------------------- 候选人回信

REPLY_SYSTEM = (
    "你是 HR 招聘机器人的邮件理解引擎。判断候选人对面试邀约的意向，"
    "并抽取其期望的新时间。只输出 JSON 对象，不要解释。"
)


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
"""


# --------------------------------------------------------------------- 语义匹配

SEMANTIC_SYSTEM = "你是招聘筛选助手，只输出 JSON 对象，不要解释。"


def build_semantic_prompt(
    *,
    company_name: str,
    position_title: str,
    position_description: str,
    resume_summary: str,
    skills: list[str],
    project_highlights: list[str],
) -> str:
    """与 ``prompt/screening.md`` 第 4 节保持一致的语义打分提示。"""
    return f"""你是{company_name}的招聘助理，请判断候选人简历与岗位的匹配程度。

岗位：{position_title}
岗位描述：{position_description}

候选人简历摘要：
{resume_summary}

技能：{'、'.join(skills) or '（未提取）'}
项目：{'；'.join(project_highlights) or '（未提取）'}

请只输出 JSON：{{"semantic_score": 0-100 的整数, "reason": "不超过 60 字的中文理由"}}
评分口径：
- 90-100：经历高度对口，可直接面试
- 70-89：主要能力对口，有小幅差距
- 50-69：部分对口，需要培养
- 0-49：明显不对口
"""
