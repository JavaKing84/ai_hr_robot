"""飞书自然语言助手的「计划」模型。

设计原则（与项目其余部分一致）：
模型只负责**理解**（这句人话想干什么、涉及谁、什么时间），
**执行**永远落回确定性代码（FeedbackService / NotificationService），
所以这里只有「意图 + 槽位」，没有任何可以直接改库的字段。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentIntent(StrEnum):
    """模型可以返回的意图集合。新增意图时必须同时改 ``WRITE_INTENTS`` / ``READ_INTENTS``。"""

    # ---- 写操作：由确定性代码执行，模型只负责归类与抽槽位 ----
    SCHEDULE = "schedule"          # 安排面试（名单 + 时间）
    RESCHEDULE = "reschedule"      # 改期（同意候选人或给新时间）
    CONFIRM = "confirm"            # 确认发送挂起的面试通知
    CANCEL = "cancel"              # 取消挂起的面试通知
    REJECT = "reject"              # 淘汰某个候选人（只改状态，不发感谢信）
    SCREEN_TOP = "screen_top"      # 从今天收到的简历里挑出前 N 份（带简历链接）
    JOB_POSTING = "job_posting"    # 主管发来招聘需求文本/截图，解析成岗位配置

    # ---- 读操作：检索真实数据后由模型组织语言 ----
    QUERY_CANDIDATES = "query_candidates"  # 有哪些候选人 / 名单
    QUERY_CANDIDATE = "query_candidate"    # 某个人的详情
    EXPLAIN_SCORE = "explain_score"        # 为什么得这个分
    QUERY_STATS = "query_stats"            # 本轮/累计统计
    QUERY_RESUME = "query_resume"          # 基于简历正文回答问题
    CHAT = "chat"                          # 闲聊 / 问机器人能干什么
    UNKNOWN = "unknown"                    # 没听懂，走原有兜底提示


WRITE_INTENTS: frozenset[AgentIntent] = frozenset(
    {
        AgentIntent.SCHEDULE,
        AgentIntent.RESCHEDULE,
        AgentIntent.CONFIRM,
        AgentIntent.CANCEL,
        AgentIntent.REJECT,
        AgentIntent.SCREEN_TOP,
        AgentIntent.JOB_POSTING,
    }
)

READ_INTENTS: frozenset[AgentIntent] = frozenset(
    {
        AgentIntent.QUERY_CANDIDATES,
        AgentIntent.QUERY_CANDIDATE,
        AgentIntent.EXPLAIN_SCORE,
        AgentIntent.QUERY_STATS,
        AgentIntent.QUERY_RESUME,
        AgentIntent.CHAT,
    }
)

#: 模型（尤其国内小模型）经常返回中文意图名。这里做一次归一化，
#: 避免因为「安排面试」和 "schedule" 的措辞差异把消息判成 unknown。
_INTENT_ALIASES: dict[str, AgentIntent] = {
    # 安排
    "schedule": AgentIntent.SCHEDULE,
    "schedule_interview": AgentIntent.SCHEDULE,
    "arrange": AgentIntent.SCHEDULE,
    "安排面试": AgentIntent.SCHEDULE,
    "面试安排": AgentIntent.SCHEDULE,
    "约面试": AgentIntent.SCHEDULE,
    "通知面试": AgentIntent.SCHEDULE,
    # 改期
    "reschedule": AgentIntent.RESCHEDULE,
    "postpone": AgentIntent.RESCHEDULE,
    "改期": AgentIntent.RESCHEDULE,
    "调整时间": AgentIntent.RESCHEDULE,
    "改时间": AgentIntent.RESCHEDULE,
    # 确认 / 取消
    "confirm": AgentIntent.CONFIRM,
    "approve": AgentIntent.CONFIRM,
    "确认": AgentIntent.CONFIRM,
    "确认发送": AgentIntent.CONFIRM,
    "cancel": AgentIntent.CANCEL,
    "取消": AgentIntent.CANCEL,
    # 淘汰
    "reject": AgentIntent.REJECT,
    "eliminate": AgentIntent.REJECT,
    "淘汰": AgentIntent.REJECT,
    "不合适": AgentIntent.REJECT,
    "不通过": AgentIntent.REJECT,
    # 筛选前 N 份
    "screen_top": AgentIntent.SCREEN_TOP,
    "screen": AgentIntent.SCREEN_TOP,
    "筛选": AgentIntent.SCREEN_TOP,
    "挑简历": AgentIntent.SCREEN_TOP,
    "筛几份": AgentIntent.SCREEN_TOP,
    # 招聘需求接入
    "job_posting": AgentIntent.JOB_POSTING,
    "jd": AgentIntent.JOB_POSTING,
    "recruitment_request": AgentIntent.JOB_POSTING,
    "招聘需求": AgentIntent.JOB_POSTING,
    "岗位要求": AgentIntent.JOB_POSTING,
    # 查询
    "query_candidates": AgentIntent.QUERY_CANDIDATES,
    "list_candidates": AgentIntent.QUERY_CANDIDATES,
    "查候选人": AgentIntent.QUERY_CANDIDATES,
    "候选人列表": AgentIntent.QUERY_CANDIDATES,
    "名单": AgentIntent.QUERY_CANDIDATES,
    "有哪些人": AgentIntent.QUERY_CANDIDATES,
    "query_candidate": AgentIntent.QUERY_CANDIDATE,
    "candidate_detail": AgentIntent.QUERY_CANDIDATE,
    "候选人详情": AgentIntent.QUERY_CANDIDATE,
    "查详情": AgentIntent.QUERY_CANDIDATE,
    "explain_score": AgentIntent.EXPLAIN_SCORE,
    "explain": AgentIntent.EXPLAIN_SCORE,
    "解释分数": AgentIntent.EXPLAIN_SCORE,
    "为什么这个分": AgentIntent.EXPLAIN_SCORE,
    "打分依据": AgentIntent.EXPLAIN_SCORE,
    "query_stats": AgentIntent.QUERY_STATS,
    "stats": AgentIntent.QUERY_STATS,
    "统计": AgentIntent.QUERY_STATS,
    "数据": AgentIntent.QUERY_STATS,
    "query_resume": AgentIntent.QUERY_RESUME,
    "ask_resume": AgentIntent.QUERY_RESUME,
    "简历问答": AgentIntent.QUERY_RESUME,
    "简历里": AgentIntent.QUERY_RESUME,
    "chat": AgentIntent.CHAT,
    "smalltalk": AgentIntent.CHAT,
    "闲聊": AgentIntent.CHAT,
    "帮助": AgentIntent.CHAT,
    "你能干什么": AgentIntent.CHAT,
    "unknown": AgentIntent.UNKNOWN,
    "other": AgentIntent.UNKNOWN,
    "其他": AgentIntent.UNKNOWN,
    "没听懂": AgentIntent.UNKNOWN,
}


def normalize_intent(raw: Any) -> AgentIntent:
    """把模型返回的任意写法归一成 ``AgentIntent``；认不出来就是 UNKNOWN。"""
    text = str(raw or "").strip()
    if not text:
        return AgentIntent.UNKNOWN
    lowered = text.lower()
    if lowered in _INTENT_ALIASES:
        return _INTENT_ALIASES[lowered]
    # 兼容 "安排面试(schedule)" 这类混写：先按子串找，长的优先
    for alias in sorted(_INTENT_ALIASES, key=len, reverse=True):
        if alias in lowered:
            return _INTENT_ALIASES[alias]
    try:
        return AgentIntent(lowered)
    except ValueError:
        return AgentIntent.UNKNOWN


class AgentPlan(BaseModel):
    """一次意图路由的结果。"""

    intent: AgentIntent = AgentIntent.UNKNOWN
    #: 消息里点到的候选人姓名（写操作必须来自当前名单，读操作可以指向历史候选人）
    names: list[str] = Field(default_factory=list)
    #: 原始时间表达（如「下周三下午两点」），交给 core.timeparse 解析，模型不换算 ISO
    time_text: str = ""
    location: str = ""
    #: 读操作时用于检索的问题改写（把「他」「那个」替换成具体对象）
    question: str = ""
    #: screen_top 专用：「筛选 5 份」里的 5；0 表示没说数量
    count: int = Field(default=0, ge=0)
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    method: Literal["llm", "rule"] = "rule"
    #: 模型原始意图文本，仅用于排查（不对外展示）
    raw_intent: str = ""

    @property
    def is_write(self) -> bool:
        return self.intent in WRITE_INTENTS

    @property
    def is_read(self) -> bool:
        return self.intent in READ_INTENTS


class AgentFacts(BaseModel):
    """回答前检索到的真实数据（防止模型编造的关键）。"""

    summary: str = ""
    rows: list[dict[str, Any]] = Field(default_factory=list)
    resume_excerpt: str = ""
    history: list[str] = Field(default_factory=list)

    def as_prompt_json(self) -> str:
        """给模型的紧凑 JSON（不含空字段，省 token）。"""
        import json

        payload: dict[str, Any] = {}
        if self.summary:
            payload["summary"] = self.summary
        if self.rows:
            payload["rows"] = self.rows
        if self.resume_excerpt:
            payload["resume_excerpt"] = self.resume_excerpt
        if self.history:
            payload["recent_dialogue"] = self.history
        return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "AgentFacts",
    "AgentIntent",
    "AgentPlan",
    "READ_INTENTS",
    "WRITE_INTENTS",
    "normalize_intent",
]
