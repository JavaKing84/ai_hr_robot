"""结构化抽取：同一套管道同时处理简历、主管消息、候选人回信。

优先级：**LLM 可用 → 大模型抽取 + 规则校验**；**LLM 不可用 → 确定性规则抽取**。
两条路径都返回同一套 Pydantic schema，业务层不感知来源（``method`` 字段留痕）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Sequence

from client.llm import LLMClient
from client.resume_parser import ResumeParser
from config.settings import Settings
from core.exceptions import ExternalServiceError, LLMUnavailable
from core.logging import get_logger, log_extra
from core.timeparse import DEFAULT_TZ, now_local, parse_chinese_datetime, parse_reschedule_time, to_utc_naive
from prompt.extraction import (
    INTERVIEW_SYSTEM,
    REPLY_SYSTEM,
    RESUME_SYSTEM,
    build_candidate_reply_prompt,
    build_interview_prompt,
    build_resume_prompt,
)
from schema.extraction import (
    CandidateReplySchema,
    ExtractedCandidate,
    InterviewScheduleSchema,
    SourceQuote,
)
from schema.message import InboundMessage

logger = get_logger(__name__)

DECLINE_KEYWORDS = (
    "放弃",
    "不参加",
    "不去了",
    "去不了",
    "已找到工作",
    "找到工作",
    "已入职",
    "另谋",
    "无法参加",
    "不考虑了",
    "已经有工作",
    "不面了",
)
RESCHEDULE_KEYWORDS = (
    "改期",
    "改到",
    "改一下",
    "换个时间",
    "换一个时间",
    "调整时间",
    "推迟",
    "延期",
    "延后",
    "提前",
    "不方便",
    "有会",
    "开会",
    "冲突",
    "没时间",
    "能否改",
    "能不能改",
    "另约",
    "改约",
)
CONFIRM_KEYWORDS = ("确认", "准时", "收到", "可以参加", "没问题", "如期", "会按时", "好的，我会")

_SENTENCE_SPLIT_RE = re.compile(r"[。！!？?\n；;]+")


# --------------------------------------------------------------------- 工具


def match_roster_names(text: str, roster: Iterable[str]) -> list[str]:
    """在自由文本里按「已知候选人名单」做接地匹配，按出现位置排序。

    接地匹配比「猜姓名」可靠得多：主管回复的姓名一定来自本轮已发的名单，
    这样既不会把“下周”“技术部”误认成人名，也能正确处理顿号/空格/换行混排。
    """
    positions: list[tuple[int, str]] = []
    for name in roster:
        if not name:
            continue
        index = text.find(name)
        if index >= 0:
            positions.append((index, name))
    positions.sort(key=lambda item: item[0])
    seen: set[str] = set()
    ordered: list[str] = []
    for _, name in positions:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def detect_intent(text: str) -> str:
    lowered = text or ""
    if any(keyword in lowered for keyword in DECLINE_KEYWORDS):
        return "decline"
    if any(keyword in lowered for keyword in RESCHEDULE_KEYWORDS):
        return "reschedule"
    if any(keyword in lowered for keyword in CONFIRM_KEYWORDS):
        return "confirm"
    return "other"


def _first_sentence(text: str, keyword: str) -> str:
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        if keyword in sentence:
            return sentence.strip()
    return ""


def _merge_resume(rule: ExtractedCandidate, llm: ExtractedCandidate) -> ExtractedCandidate:
    """合并「规则抽取」与「大模型抽取」，按字段性质决定谁优先。

    2026-11-12 调整（原实现是模型全覆盖规则，模型写错姓名/邮箱就直接错）：

    * **基础字段**（姓名/邮箱/电话/学历/院校/专业）→ **规则优先**。这些格式固定，
      正则与词典校验比模型可靠；模型只在规则抽空时补位。
    * **年限** → **规则优先**。规则用的是「工作时间区间并集」，比模型读文字描述准；
      原实现取 ``max(rule, llm)``，模型虚报年限会让硬条件误放行。
    * **复杂字段**（技能/项目亮点/摘要/岗位意向）→ **模型优先**，规则兜底。
      「我负责写代码」这类表述只有模型能归纳成「后端开发」。
    """

    def rule_first(field: str):
        rule_value = getattr(rule, field)
        llm_value = getattr(llm, field)
        if isinstance(rule_value, str):
            return rule_value.strip() or (llm_value or "")
        return rule_value if rule_value else llm_value

    def llm_first(field: str):
        llm_value = getattr(llm, field)
        rule_value = getattr(rule, field)
        if isinstance(llm_value, str):
            return llm_value.strip() or (rule_value or "")
        return llm_value if llm_value else rule_value

    # 年限：规则抽到就用规则的；规则为 0（确实是应届/没写区间）才信模型
    rule_years = float(rule.years_experience or 0)
    years = rule_years if rule_years > 0 else float(llm.years_experience or 0)

    # 技能：模型先（保留更自然的表述与同义词），规则命中的词补在后面，顺序去重
    skills = list(dict.fromkeys([*(llm.skills or []), *(rule.skills or [])]))

    return ExtractedCandidate(
        name=rule_first("name"),
        email=rule_first("email"),
        phone=rule_first("phone"),
        education_level=rule_first("education_level"),
        school=rule_first("school"),
        major=rule_first("major"),
        years_experience=years,
        skills=skills,
        project_highlights=llm_first("project_highlights"),
        summary=llm_first("summary"),
        position_hint=llm_first("position_hint"),
        confidence=max(llm.confidence, rule.confidence),
        source_quotes=llm.source_quotes or rule.source_quotes,
    )


# --------------------------------------------------------------------- 服务


class ExtractionService:
    def __init__(self, settings: Settings, *, llm: LLMClient | None = None, parser: ResumeParser | None = None) -> None:
        self.settings = settings
        self.llm = llm
        self.parser = parser or ResumeParser(settings)

    @property
    def tz(self) -> str:
        return self.settings.timezone or DEFAULT_TZ

    # ------------------------------------------------------------ 简历抽取
    async def extract_candidate(
        self,
        resume_text: str,
        *,
        filename: str = "",
        positions: Sequence[tuple[str, str]] = (),
    ) -> ExtractedCandidate:
        rule_result = self.parser.prefill(resume_text, filename)

        if self.llm is None or not self.llm.enabled:
            return rule_result

        try:
            data = await self.llm.complete_json(
                system=RESUME_SYSTEM,
                user=build_resume_prompt(resume_text, list(positions)),
            )
            llm_result = ExtractedCandidate.model_validate(data)
        except (ExternalServiceError, LLMUnavailable) as exc:
            logger.warning("extract.resume.llm_failed", extra=log_extra(error=str(exc)))
            return rule_result
        except Exception as exc:  # noqa: BLE001 - schema 校验失败同样降级
            logger.warning("extract.resume.schema_invalid", extra=log_extra(error=str(exc)))
            return rule_result

        return _merge_resume(rule_result, llm_result)

    # ------------------------------------------------------- 主管面试安排
    async def extract_interview_schedule(
        self,
        message: InboundMessage,
        *,
        roster: Sequence[str],
        position_title: str = "",
        location_default: str = "",
        now: datetime | None = None,
    ) -> InterviewScheduleSchema:
        local_now = now or now_local(self.tz)
        text = message.text or ""

        rule_names = match_roster_names(text, roster)
        rule_time = parse_chinese_datetime(text, now=local_now, tz=self.tz)
        rule_result = InterviewScheduleSchema(
            candidate_names=rule_names,
            interview_time=rule_time,
            location="",
            position_title=position_title,
            confidence=0.45 if (rule_names or rule_time) else 0.1,
            source_quotes=[
                SourceQuote(field="candidate_names", quote=_first_sentence(text, "通知") or text[:60])
            ]
            if rule_names
            else [],
            method="rule",
        )

        if self.llm is None or not self.llm.enabled:
            return rule_result

        try:
            data = await self.llm.complete_json(
                system=INTERVIEW_SYSTEM,
                user=build_interview_prompt(
                    text,
                    candidates=[(name, "") for name in roster],
                    now_local=local_now,
                    position_title=position_title,
                    location_default=location_default,
                    source_channel="邮件" if message.is_email else "飞书",
                ),
            )
            # 模型偶尔返回“跑题”的 JSON：连关键字段都没有就直接判为无效，回落规则
            if not {"candidate_names", "interview_time"} & set(data):
                raise ExternalServiceError(f"LLM 返回缺少关键字段: {sorted(data)[:6]}")
            llm_result = InterviewScheduleSchema.model_validate(data)
        except (ExternalServiceError, LLMUnavailable) as exc:
            logger.warning("extract.schedule.llm_failed", extra=log_extra(error=str(exc)))
            return rule_result
        except Exception as exc:  # noqa: BLE001
            logger.warning("extract.schedule.schema_invalid", extra=log_extra(error=str(exc)))
            return rule_result

        # 接地校验：模型给的姓名必须在名单里，否则回落到规则结果
        valid_names = [name for name in llm_result.candidate_names if name in set(roster)]
        if not valid_names:
            valid_names = rule_names
        llm_result.candidate_names = valid_names
        llm_result.method = "llm"
        if llm_result.interview_time is None:
            llm_result.interview_time = rule_time
        elif llm_result.interview_time.tzinfo is None:
            llm_result.interview_time = llm_result.interview_time.replace(tzinfo=local_now.tzinfo)
        if not llm_result.position_title:
            llm_result.position_title = position_title
        return llm_result

    # ------------------------------------------------------- 候选人回信
    async def extract_candidate_reply(
        self,
        body_text: str,
        *,
        candidate_name: str = "",
        position_title: str = "",
        current_interview_time: str = "",
        now: datetime | None = None,
    ) -> CandidateReplySchema:
        local_now = now or now_local(self.tz)
        intent = detect_intent(body_text)
        proposed = parse_reschedule_time(body_text, now=local_now, tz=self.tz)
        rule_result = CandidateReplySchema(
            intent=intent,  # type: ignore[arg-type]
            proposed_time=proposed,
            reason="",
            confidence=0.5 if intent != "other" else 0.2,
            source_quotes=[SourceQuote(field="intent", quote=_first_sentence(body_text, "改") or body_text[:60])]
            if intent != "other"
            else [],
            method="rule",
        )

        if self.llm is None or not self.llm.enabled:
            return rule_result

        try:
            data = await self.llm.complete_json(
                system=REPLY_SYSTEM,
                user=build_candidate_reply_prompt(
                    body_text,
                    candidate_name=candidate_name,
                    position_title=position_title,
                    current_interview_time=current_interview_time,
                    now_local=local_now,
                ),
            )
            if "intent" not in data:
                raise ExternalServiceError(f"LLM 返回缺少 intent: {sorted(data)[:6]}")
            llm_result = CandidateReplySchema.model_validate(data)
        except (ExternalServiceError, LLMUnavailable) as exc:
            logger.warning("extract.reply.llm_failed", extra=log_extra(error=str(exc)))
            return rule_result
        except Exception as exc:  # noqa: BLE001
            logger.warning("extract.reply.schema_invalid", extra=log_extra(error=str(exc)))
            return rule_result

        llm_result.method = "llm"
        if llm_result.intent == "other" and intent != "other":
            llm_result.intent = intent  # type: ignore[assignment]
        if llm_result.proposed_time is None:
            llm_result.proposed_time = proposed
        elif llm_result.proposed_time.tzinfo is None:
            llm_result.proposed_time = llm_result.proposed_time.replace(tzinfo=local_now.tzinfo)
        return llm_result


def schedule_time_to_utc(schedule: InterviewScheduleSchema) -> datetime | None:
    """把抽取结果里的本地时间转成入库用的 naive UTC。"""
    if schedule.interview_time is None:
        return None
    return to_utc_naive(schedule.interview_time)
