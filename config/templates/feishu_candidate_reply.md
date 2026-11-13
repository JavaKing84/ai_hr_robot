{{ supervisor_title }}，{{ candidate_name }}（{{ position_title }}）回复：{{ intent_label }}。
{% if proposed_time -%}
希望改到 {{ proposed_time }}（原定 {{ current_interview_time }}）。
{% endif -%}
原话：{{ candidate_message }}
{% if action_hint -%}
{{ action_hint }}
{% endif -%}
