"""把主管发来的招聘需求（JD 文本/截图转录）解析成可执行的岗位配置。

模型只负责「读懂 JD」，产出结构化的职位草稿；字段合法性、算子白名单、
写盘位置全部由确定性代码把关（``schema/config.PositionConfig`` 校验不过就退回重发）。
"""

from __future__ import annotations

JOBS_INTAKE_SYSTEM = (
    "你是招聘系统的岗位配置解析器。把主管写的招聘需求转成一个 JSON 对象，"
    "只输出 JSON，不要解释、不要 Markdown 围栏。"
)

#: 允许出现在规则里的候选人字段与算子（与 schema/config.py 完全一致）
ALLOWED_FIELDS = (
    "education_level",
    "school",
    "major",
    "years_experience",
    "skills",
    "project_highlights",
    "summary",
)
ALLOWED_OPERATORS = (
    "gte",
    "lte",
    "eq",
    "ne",
    "in",
    "not_in",
    "contains_any",
    "contains_all",
    "exists",
)


def build_intake_prompt(text: str, *, departments: list[str], default_department: str) -> str:
    return f"""下面是一段招聘需求原文：
---
{text}
---

请把这段原文转成**一个** JSON 对象（不是数组），顶层必须同时包含这 9 个字段：

1. code：英文小写下划线编码，如 python_developer
2. title：岗位名，如 Python开发工程师
3. department：部门名，只能从「{'、'.join(departments) or default_department}」里选
4. headcount：招聘人数（整数，没写就 1）
5. description：一句话职责摘要，不超过 80 字
6. keywords：简历里出现就能归位到本岗位的词，3-8 个
7. threshold：及格分（0-100 整数，没写就 55）
8. hard_conditions：数组。每个元素是一个对象，键固定为 field / operator / value / label，
   例如 field 取 education_level、operator 取 gte、value 取 本科、label 写「学历本科及以上」
9. weighted：数组。每个元素的键固定为 field / operator / weight / value / label，
   weight 是 0-100 的数字，value 可以是字符串或字符串数组

规则：
- field 只能取：{', '.join(ALLOWED_FIELDS)}
- operator 只能取：{', '.join(ALLOWED_OPERATORS)}
- 「本科以上」「3 年以上经验」「必须重点院校」这类硬门槛放 hard_conditions；
  「熟悉 Python」「有高并发经验」这类加分项放 weighted
- weighted 各项 weight 加起来接近 100，每条 weight 必须大于 0
- 学历写中文：大专 / 本科 / 硕士 / 博士；年限写整数
- 专业、院校、技能这类「包含即算」的条件，operator 必须用 contains_any，不要用 in
- 原文没写的条件不要编，宁可少给
- 输出必须从最外层的对象开始写，第一个字符就是左花括号，最后一个字符是右花括号
"""
