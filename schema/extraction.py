"""LLM / 规则抽取的结构化 schema（借鉴 langextract 的“给定 Schema + 原文出处”设计）。

两类输入各一份 schema：
- ``InterviewScheduleSchema``：主管的面试安排（飞书自由文本 / 邮件回复）
- ``CandidateReplySchema``：候选人的回信意图（改期 / 放弃 / 确认）
以及简历结构化抽取 ``ExtractedCandidate``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SourceQuote(BaseModel):
    """原文出处：``field`` 指向抽出的字段，``quote`` 是支撑它的原句。"""

    field: str
    quote: str


class ExtractedCandidate(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    education_level: str = Field(default="", description="大专 / 本科 / 硕士 / 博士")
    school: str = ""
    major: str = ""
    years_experience: float = 0.0
    skills: list[str] = Field(default_factory=list)
    project_highlights: list[str] = Field(default_factory=list)
    summary: str = ""
    position_hint: str = Field(default="", description="简历里写明的应聘岗位（可为空）")
    confidence: float = Field(default=0.0, ge=0, le=1)
    source_quotes: list[SourceQuote] = Field(default_factory=list)


class InterviewScheduleSchema(BaseModel):
    """主管给出的面试安排。"""

    candidate_names: list[str] = Field(default_factory=list, description="主管点名要面试的候选人姓名")
    interview_time: datetime | None = Field(default=None, description="面试时间（本地时区）")
    location: str = ""
    position_title: str = ""
    notes: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    source_quotes: list[SourceQuote] = Field(default_factory=list)
    #: 抽取方式：rule（确定性规则）/ llm（大模型）
    method: Literal["rule", "llm"] = "rule"

    @property
    def has_schedule(self) -> bool:
        return bool(self.candidate_names) and self.interview_time is not None


class CandidateReplySchema(BaseModel):
    """候选人回信意图。"""

    intent: Literal["confirm", "reschedule", "decline", "other"] = "other"
    proposed_time: datetime | None = Field(default=None, description="候选人希望的新时间")
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    source_quotes: list[SourceQuote] = Field(default_factory=list)
    method: Literal["rule", "llm"] = "rule"
