"""HTTP 层请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from core.timeparse import format_interview_time
from model.candidate import Candidate, CandidateStatus


class CandidateRead(BaseModel):
    id: int
    name: str
    email: str
    phone: str
    position_code: str
    department: str
    education_level: str
    school: str
    major: str
    years_experience: float
    skills: list[str] = Field(default_factory=list)
    project_highlights: list[str] = Field(default_factory=list)
    score: float
    status: str
    status_label: str
    hard_failed: list[str] = Field(default_factory=list)
    is_duplicate: bool = False
    interview_time: datetime | None = None
    interview_time_display: str = ""
    resume_filename: str = ""
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, candidate: Candidate, tz: str = "Asia/Shanghai") -> "CandidateRead":
        return cls(
            id=candidate.id,
            name=candidate.name,
            email=candidate.email,
            phone=candidate.phone,
            position_code=candidate.position_code,
            department=candidate.department,
            education_level=candidate.education_level,
            school=candidate.school,
            major=candidate.major,
            years_experience=candidate.years_experience,
            skills=list(candidate.skills or []),
            project_highlights=list(candidate.project_highlights or []),
            score=round(candidate.score or 0, 2),
            status=candidate.status,
            status_label=CandidateStatus.LABELS.get(candidate.status, candidate.status),
            hard_failed=list(candidate.hard_failed or []),
            is_duplicate=candidate.is_duplicate,
            interview_time=candidate.interview_time,
            interview_time_display=format_interview_time(candidate.interview_time, tz)
            if candidate.interview_time
            else "",
            resume_filename=candidate.resume_filename,
            created_at=candidate.created_at,
        )


class CandidateListResponse(BaseModel):
    total: int
    items: list[CandidateRead]


class EventRead(BaseModel):
    id: int
    action: str
    actor: str
    object_type: str
    object_id: int | None
    from_state: str
    to_state: str
    detail: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    occurred_at: datetime


class PositionRead(BaseModel):
    code: str
    title: str
    department: str
    headcount: int
    threshold: float
    rule_version: str
    supervisor_name: str
    supervisor_title: str
    supervisor_email: str
    active: bool


class IngestResponse(BaseModel):
    candidate_id: int
    name: str
    position_code: str
    status: str
    score: float
    is_duplicate: bool
    message: str = ""


class IngestTextRequest(BaseModel):
    """手动导入：直接提交简历纯文本（便于联调与测试，不需要真实文件）。"""

    text: str = Field(min_length=20, description="简历纯文本")
    filename: str = "manual-resume.txt"
    position_code: str | None = None

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text 不能为空")
        return value


class PackageDispatchResult(BaseModel):
    position_code: str
    position_title: str
    supervisor: str
    received_count: int
    passed_count: int
    candidate_names: list[str] = Field(default_factory=list)
    package_path: str = ""
    feishu_message_id: str = ""
    email_message_id: str = ""
    ok: bool = True
    error: str = ""


class RunCycleResponse(BaseModel):
    cycle_id: int
    triggered_at: datetime
    ingested: int
    parsed: int
    screened_passed: int
    screened_rejected: int
    duplicates: int
    packages: list[PackageDispatchResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class EmailPollResponse(BaseModel):
    processed: int = 0
    ignored: int = 0
    failed: int = 0
    details: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SimulateInboundEmailRequest(BaseModel):
    """演示 / 联调用：把一封「收到的邮件」直接投进处理管道。"""

    from_address: str
    subject: str = ""
    body: str
    in_reply_to: str = ""
    references: list[str] = Field(default_factory=list)
    to_addresses: list[str] = Field(default_factory=list)
    message_id: str = ""

    @field_validator("body")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body 不能为空")
        return value


class EmailTestRequest(BaseModel):
    to: str
    subject: str = "【测试】ai-hr 邮件通道自检"
    body: str = "这是一封来自 ai-hr 机器人邮件通道的自检邮件。"


class EmailTestResponse(BaseModel):
    ok: bool
    mode: str
    message_id: str = ""
    error: str = ""


class FeishuWebhookResponse(BaseModel):
    ok: bool = True
    event_type: str = ""
    handled: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class MessageRead(BaseModel):
    """收发留痕（mock 模式下就是“发出的飞书消息/邮件”的可读副本）。"""

    id: int
    channel: str
    direction: str
    sender: str
    recipient: str
    subject: str = ""
    body: str = ""
    external_id: str = ""
    status: str = ""
    candidate_id: int | None = None
    position_id: int | None = None
    parsed: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: datetime | None = None
