{{ supervisor_title }}，已通知 {{ candidate_count }} 人：{% for c in candidates %}{{ c.name }}{% if not loop.last %}、{% endif %}{% endfor %}。
时间 {{ interview_time }}，地点 {{ location }}，岗位 {{ position_title }}。
候选人回信改期或放弃，我在这里报给你。
