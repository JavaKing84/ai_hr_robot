{{ supervisor_title }}，已按您的安排通知以下 {{ candidate_count }} 位候选人：
{% for c in candidates -%}
- {{ c.name }}（{{ c.email }}）
{% endfor %}
面试时间：{{ interview_time }}
面试地点：{{ location }}
面试岗位：{{ position_title }}
邮件已发出，模板为标准面试通知；候选人回复改期/放弃时，我会第一时间在这里向您汇报。

—— {{ company_name }} HR 机器人
