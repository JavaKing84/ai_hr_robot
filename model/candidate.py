"""候选人聚合根：简历、结构化结果、命中明细、状态与去重标记。

去重策略（借鉴 frappe/hrms 业务逻辑，不复制代码）：同一「邮箱 + 手机号」重复投递时
保留首次记录，把本次投递作为事件追加，``is_duplicate`` / ``duplicate_of_id`` 标出关系，
``last_seen_at`` 刷新——历史不丢、状态机不被覆盖。

索引（2026-11-14 按真实查询重排，只为「代码里真的会跑的 SQL」建索引）：
- ``(status, position_code, id)``：列表页 状态 + 职位 组合筛选；
- ``(position_id, status, score)``：按职位取「在流程中的候选人」并按分数排序；
- ``cycle_id`` / ``resume_sha256`` / ``email`` / ``phone`` / ``name``：去重与点名查询；
- ``interview_time`` 部分索引：只索引「已排期」的行，扫面试日程不必回全表；
- ``skills`` GIN（jsonb_path_ops）：按技能检索简历。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import JSONBType, TimestampMixin, UTCDateTime


class CandidateStatus:
    """状态机常量（简化版，对应产品文档第 6 节）。"""

    RECEIVED = "received"                      # 已收取
    PARSED = "parsed"                          # 已解析
    SCREENED_PASSED = "screened_passed"        # 已筛选（通过）
    SCREENED_REJECTED = "screened_rejected"    # 已筛选（淘汰，不再进入后续）
    DISPATCHED = "dispatched"                  # 已发主管
    SUPERVISOR_FEEDBACK = "supervisor_feedback"  # 主管已反馈（进入面试名单）
    INTERVIEW_NOTIFIED = "interview_notified"   # 已邮件通知
    INTERVIEW_PENDING_CONFIRM = "interview_pending_confirm"  # 面试通知已生成，等 HR 确认后发送
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
        INTERVIEW_PENDING_CONFIRM,
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
        INTERVIEW_PENDING_CONFIRM: "面试通知待确认",
        RESCHEDULE_PENDING: "改期中",
        DECLINED: "已放弃",
        COMPLETED: "已完成",
    }


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        Index("ix_candidates_status_position_id", "status", "position_code", "id"),
        Index("ix_candidates_position_status_score", "position_id", "status", "score"),
        Index("ix_candidates_cycle_id", "cycle_id", "id"),
        Index("ix_candidates_email", "email"),
        Index("ix_candidates_phone", "phone"),
        Index("ix_candidates_name", "name"),
        Index("ix_candidates_resume_sha256", "resume_sha256"),
        Index(
            "ix_candidates_interview_time",
            "interview_time",
            postgresql_where=text("interview_time IS NOT NULL"),
        ),
        Index(
            "ix_candidates_skills_gin",
            "skills",
            postgresql_using="gin",
            postgresql_ops={"skills": "jsonb_path_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 身份与联系方式
    name: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")

    # 职位归位（冗余存 code / department，避免异步懒加载）
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    position_code: Mapped[str] = mapped_column(String(64), default="")
    department: Mapped[str] = mapped_column(String(64), default="")

    # 结构化字段（LLM 抽取 + 规则兜底）
    education_level: Mapped[str] = mapped_column(String(16), default="")
    school: Mapped[str] = mapped_column(String(128), default="")
    major: Mapped[str] = mapped_column(String(128), default="")
    years_experience: Mapped[float] = mapped_column(Float, default=0.0)
    skills: Mapped[list[str]] = mapped_column(JSONBType, default=list)
    project_highlights: Mapped[list[str]] = mapped_column(JSONBType, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)

    # 简历文件：path 存仓库相对路径（var/resumes/xxx.pdf），避免移动项目后失效
    resume_path: Mapped[str] = mapped_column(String(512), default="")
    resume_filename: Mapped[str] = mapped_column(String(255), default="")
    resume_text: Mapped[str] = mapped_column(Text, default="")
    # 内容指纹：按文件内容去重。server_default 让「不带这一列的裸 INSERT / 数据搬迁」
    # 也能落库，不必依赖 ORM 把 Python 默认值一起发过去。
    resume_sha256: Mapped[str] = mapped_column(String(64), default="", server_default="")
    resume_size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parse_engine: Mapped[str] = mapped_column(
        String(32), default="", server_default=""
    )  # pypdf / python-docx / unstructured
    source: Mapped[str] = mapped_column(String(32), default="imap")  # imap / manual

    # 筛选结果
    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_detail: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)
    hard_failed: Mapped[list[str]] = mapped_column(JSONBType, default=list)

    # 状态与去重
    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.RECEIVED)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # 面试安排
    interview_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    interview_location: Mapped[str] = mapped_column(String(255), default="")
    cycle_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Candidate {self.id} {self.name} {self.status}>"
