{{ supervisor_title }}，{{ position_title }} 岗位的候选人 {{ candidate_name }} 有新的回复，向您汇报：

- 候选人意向：{{ intent_label }}
{% if proposed_time -%}
- 候选人希望的时间：{{ proposed_time }}
{% endif -%}
- 原定面试时间：{{ current_interview_time }}
- 候选人原文：
> {{ candidate_message }}

{% if action_hint -%}
{{ action_hint }}
{% endif -%}
—— {{ company_name }} HR 机器人
