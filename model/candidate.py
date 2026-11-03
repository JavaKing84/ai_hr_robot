"""候选人聚合根：简历、结构化结果、命中明细、状态与去重标记。

去重策略（借鉴 frappe/hrms 业务逻辑，不复制代码）：同一「邮箱 + 手机号」重复投递时
保留首次记录，把本次投递作为事件追加，``is_duplicate`` / ``duplicate_of_id`` 标出关系，
``last_seen_at`` 刷新——历史不丢、状态机不被覆盖。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import TimestampMixin


class CandidateStatus:
    """状态机常量（简化版，对应产品文档第 6 节）。"""

    RECEIVED = "received"                      # 已收取
    PARSED = "parsed"                          # 已解析
    SCREENED_PASSED = "screened_passed"        # 已筛选（通过）
    SCREENED_REJECTED = "screened_rejected"    # 已筛选（淘汰，不再进入后续）
    DISPATCHED = "dispatched"                  # 已发主管
    SUPERVISOR_FEEDBACK = "supervisor_feedback"  # 主管已反馈（进入面试名单）
    INTERVIEW_NOTIFIED = "interview_notified"   # 已邮件通知
    RESCHEDULE_PENDING = "reschedule_pending"   # 改期中（等主管确认）
    DECLINED = "declined"                       # 候选人放弃
    COMPLETED = "completed"                     # 已完成

    ALL = {
        RECEIVED,
        PARSED,
        SCREENED_PASSED,
        SCREENED_REJECTED,
        DISPATCHED,
        SUPERVISOR_FEEDBACK,
        INTERVIEW_NOTIFIED,
        RESCHEDULE_PENDING,
        DECLINED,
        COMPLETED,
    }

    LABELS = {
        RECEIVED: "已收取",
        PARSED: "已解析",
        SCREENED_PASSED: "筛选通过",
        SCREENED_REJECTED: "筛选淘汰",
        DISPATCHED: "已发主管",
        SUPERVISOR_FEEDBACK: "主管已反馈",
        INTERVIEW_NOTIFIED: "已邮件通知",
        RESCHEDULE_PENDING: "改期中",
        DECLINED: "已放弃",
        COMPLETED: "已完成",
    }


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 身份与联系方式
    name: Mapped[str] = mapped_column(String(64), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    phone: Mapped[str] = mapped_column(String(32), default="", index=True)

    # 职位归位（冗余存 code / department，避免异步懒加载）
    position_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    position_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    department: Mapped[str] = mapped_column(String(64), default="")

    # 结构化字段（LLM 抽取 + 规则兜底）
    education_level: Mapped[str] = mapped_column(String(16), default="")
    school: Mapped[str] = mapped_column(String(128), default="")
    major: Mapped[str] = mapped_column(String(128), default="")
    years_experience: Mapped[float] = mapped_column(Float, default=0.0)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    project_highlights: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # 简历文件
    resume_path: Mapped[str] = mapped_column(String(512), default="")
    resume_filename: Mapped[str] = mapped_column(String(255), default="")
    resume_text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="imap")  # imap / manual

    # 筛选结果
    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hard_failed: Mapped[list[str]] = mapped_column(JSON, default=list)

    # 状态与去重
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.RECEIVED, index=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 面试安排
    interview_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    interview_location: Mapped[str] = mapped_column(String(255), default="")
    cycle_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Candidate {self.id} {self.name} {self.status}>"
