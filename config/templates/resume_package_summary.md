# 简历筛选包 · {{ position_title }}

- 公司：{{ company_name }}
- 部门：{{ department }}
- 主管：{{ supervisor_name }}
- 生成时间：{{ generated_at }}
- 本轮收到简历：{{ received_count }} 份；通过筛选：{{ passed_count }} 份
- 筛选规则版本：{{ rule_version }}（阈值 {{ threshold }}）

回复方式：直接回复本邮件即可，例如「请通知张三、李四，下周三下午 14:00 来公司面试」。

## 通过筛选的候选人

{% for c in candidates -%}
### {{ loop.index }}. {{ c.name }}（综合分 {{ c.score }}）
- 学历：{{ c.education_level }}｜院校：{{ c.school }}｜专业：{{ c.major }}
- 经验：{{ c.years_experience }} 年｜邮箱：{{ c.email }}｜电话：{{ c.phone }}
- 技能：{{ c.skills }}
- 项目：{{ c.project_highlights }}
- 命中明细：{{ c.hit_detail }}
{% if c.deep_line %}- AI 深度解析：{{ c.deep_line }}{% endif %}
{% if c.risks %}- 待验证风险：{{ c.risks }}{% endif %}
{% if c.interview_focus %}- 建议追问：{{ c.interview_focus }}{% endif %}
- 简历文件：{{ c.resume_filename }}

{% endfor %}
