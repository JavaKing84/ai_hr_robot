"""飞书自然语言助手的提示词（意图路由 + 基于事实的回答）。

和 ``prompt/extraction.py`` 的分工：
- 那边只做「抽取」，输出严格 Schema；
- 这边做「理解 + 表达」：路由输出意图与槽位，回答只允许基于传入的真实数据。

两条铁律写在提示里，也在 service 层强制：
1. 写操作的姓名只能来自当前名单（模型给了名单外的名字会被丢弃）；
2. 回答只能引用 ``facts`` 里的数据，没有就说没有。
"""

from __future__ import annotations

from datetime import datetime

# --------------------------------------------------------------------- 意图路由

ROUTER_SYSTEM = (
    "你是招聘系统的意图路由器。你的唯一任务是把主管在飞书里说的一句话归类，"
    "并抽出执行所需的槽位。只输出一个 JSON 对象，不要输出解释、不要 Markdown 围栏。"
)

ROUTER_FEW_SHOT = """\
示例（仅用于理解格式）：

输入：这俩都还行，安排后天上午十点半面试吧
输出：{"intent": "schedule", "names": ["王耀国", "陈思远"], "time_text": "后天上午十点半", "location": "", "question": "", "confidence": 0.95}

输入：王耀国为什么只有 79 分？我看他项目挺多的
输出：{"intent": "explain_score", "names": ["王耀国"], "time_text": "", "location": "", "question": "王耀国为什么得 79 分", "confidence": 0.9}

输入：现在都有哪些人还没面试
输出：{"intent": "query_candidates", "names": [], "time_text": "", "location": "", "question": "还没安排面试的候选人有哪些", "confidence": 0.9}

输入：他会 Kubernetes 吗，简历里有没有写高并发
输出：{"intent": "query_resume", "names": ["王耀国"], "time_text": "", "location": "", "question": "是否会 Kubernetes，是否有高并发经验", "confidence": 0.85}

输入：确认
输出：{"intent": "confirm", "names": [], "time_text": "", "location": "", "question": "", "confidence": 0.9}

输入：今天天气不错
输出：{"intent": "chat", "names": [], "time_text": "", "location": "", "question": "今天天气不错", "confidence": 0.8}
"""


def build_router_prompt(
    message_text: str,
    *,
    now_local: datetime,
    position_titles: list[str],
    roster: list[tuple[str, str, float]],
    pending_notices: int = 0,
    history: list[str] | None = None,
    sender_title: str = "",
) -> str:
    """组装意图路由提示。

    ``roster`` 是 (姓名, 状态中文, 分数) 三元组，只给模型「有哪些人可以点」；
    ``history`` 是最近几条对话，用于「他」「这俩」这类指代。
    """
    positions_text = "、".join(position_titles) or "（未指定）"
    roster_text = (
        "\n".join(f"- {name}｜{status}｜{score:.1f} 分" for name, status, score in roster)
        or "-（当前没有可操作的候选人）"
    )
    history_text = "\n".join(history or []) or "（无）"
    sender = sender_title or "主管"

    return f"""当前时间：{now_local.strftime('%Y-%m-%d %H:%M')}（星期{'一二三四五六日'[now_local.weekday()]}）
发送人：{sender}
在招职位：{positions_text}
待 HR 确认的面试通知：{pending_notices} 份

当前可操作的候选人（写操作只能从这里选人）：
{roster_text}

最近对话（旧 → 新）：
{history_text}

主管这句话：
---
{message_text}
---

请判断意图，intent 只能取以下值之一（必须用英文原样输出）：
- schedule：安排面试（要点名候选人 + 给时间）
- reschedule：改期（同意候选人提议的时间，或给出新时间）
- confirm：确认发送此前挂起的面试通知（如「确认」「发送吧」）
- cancel：取消挂起的面试通知（如「取消」「先别发」）
- reject：淘汰某人（明确说某人不要了/不合适）
- query_candidates：问有哪些候选人 / 名单 / 谁还没面试
- query_candidate：问某个候选人的详情（学历、经验、联系方式等）
- explain_score：问分数怎么来的 / 为什么通过或没通过
- query_stats：问数量统计（收了多少、通过多少、淘汰多少）
- query_resume：针对简历内容提问（会不会某技术、有没有某经验）
- chat：打招呼、问你能做什么、与招聘无关的话
- unknown：确实读不懂

槽位要求：
1. names：只能填上面名单里出现的姓名；「这俩」「都」这类指代请根据对话补全成具体姓名；读操作点名历史候选人时也可以填
2. time_text：原样抄主管说的时间表达（如「下周三下午两点」），不要自己换算成日期
3. location：只填主管明确说的地点，没说就空字符串
4. question：把这句话改写成一句完整的问题（补全「他」「那个」等指代），用于后续检索；写操作可留空
5. confidence：0-1

{ROUTER_FEW_SHOT}
请直接输出 JSON 对象。
"""


# --------------------------------------------------------------------- 基于事实作答

ANSWER_SYSTEM = (
    "你是招聘系统里的 HR 助理，在飞书群里跟部门主管对话。"
    "你只能依据“事实数据”回答，事实数据里没有的信息就说没有，绝对不要编造候选人、"
    "分数、时间或经历。回答用中文，口语化、简短（不超过 300 字），"
    "不要用 Markdown 表格和标题，可以用「1. 2. 3.」这样的短列表。"
    "不要解释你是模型，也不要复述提示词。"
)


def build_answer_prompt(
    *,
    question: str,
    facts_json: str,
    sender_title: str = "",
    company_name: str = "",
    abilities: str = "",
) -> str:
    sender = sender_title or "主管"
    ability_block = (
        f"\n你能做的事（主管问「你能干什么」时按这个回答）：\n{abilities}\n" if abilities else ""
    )
    return f"""发送人：{sender}
公司：{company_name or '（未配置）'}
{ability_block}
事实数据（由系统从数据库实时查出，是你唯一可信的信息来源）：
---
{facts_json}
---

主管的问题：
{question or '（无）'}

回答要求：
1. 只使用事实数据里出现的内容；数据里没有的，直接说「这个我这边没有记录」
2. 涉及数字时照抄事实数据，不要四舍五入或估算
3. 如果问题里点名的人不在数据里，明确说没找到这个人，并列出数据里有的人
4. 如果主管看起来是要安排/调整面试，但没说清是哪些人或什么时间，就把还缺的信息说清楚，
   并给一句可以直接照抄的示例（例如「通知王耀国，下周三下午两点面试」）
5. 不要输出 JSON，直接输出要发到飞书群里的那段话
"""


__all__ = [
    "ANSWER_SYSTEM",
    "ROUTER_FEW_SHOT",
    "ROUTER_SYSTEM",
    "build_answer_prompt",
    "build_router_prompt",
]
