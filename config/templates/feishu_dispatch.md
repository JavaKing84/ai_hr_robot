{{ supervisor_title }}，{{ position_title }}：收到 {{ received_count }} 份，通过 {{ passed_count }} 份。简历包 {{ package_name }} 已发您邮箱。

{% for c in candidates -%}
{{ loop.index }}. {{ c.name }} {{ c.score }} 分｜{{ c.education_level }}·{{ c.school }}·{{ c.years_experience }} 年
{% endfor %}
回复「通知张三、李四，下周三下午两点」即可安排面试。
