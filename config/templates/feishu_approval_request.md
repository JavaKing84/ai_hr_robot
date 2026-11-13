{{ supervisor_title }}，{{ count }} 份面试通知待确认（一封都没发）：
{% for c in items -%}
{{ c.index }}. {{ c.name }}｜{{ c.email }}
{% endfor -%}
时间：{{ interview_time }}　地点：{{ location }}
回复「确认」全发；「确认 1,3」只发指定；「取消」全不发。
