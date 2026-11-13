{{ supervisor_title }}，有 {{ count }} 份面试通知等您确认，**目前一封都没有发出去**。

{% for c in items -%}
{{ c.index }}. {{ c.name }}｜{{ c.position_title }}｜{{ c.score }} 分
   收件邮箱：{{ c.email }}
{% endfor %}
面试时间：{{ interview_time }}
面试地点：{{ location }}

请直接回复：
- 「确认」= 全部发送
- 「确认 1」或「确认 1,3」= 只发指定序号
- 「取消」= 全部不发送

在您回复之前，候选人不会收到任何邮件。

—— {{ company_name }} HR 机器人
