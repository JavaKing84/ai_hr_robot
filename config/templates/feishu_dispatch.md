{{ supervisor_title }}，本次贵部门面试岗位 {{ position_title }}，共收到 {{ received_count }} 份简历，经过智能筛选符合条件的简历为 {{ passed_count }} 份，已经发送到您邮箱，请注意处理查收；

（简历压缩包同时附在本条飞书消息中：{{ package_name }}）

筛选结果：
{% for c in candidates -%}
{{ loop.index }}. {{ c.name }}｜{{ c.education_level }}｜{{ c.school }}｜{{ c.major }}｜{{ c.years_experience }} 年经验｜综合分 {{ c.score }}
{% endfor %}

回复方式（二选一，机器人都会处理）：
1. 直接回复本飞书消息，例如：“请通知张三、李四，下周三下午 14:00 来公司面试”
2. 直接在收到的简历邮件上回复同样的内容

—— {{ company_name }} HR 机器人
