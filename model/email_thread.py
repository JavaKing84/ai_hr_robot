"""邮件线程：持久化原始 Message-ID，后续发送带 In-Reply-To / References。

设计参考 Sentry 的 ``GroupEmailThread``（只借鉴模型设计，不复制代码）：
- ``root_message_id``：线程根，回复邮件靠它挂回业务实体
- ``message_ids``：本线程全部 Message-ID，便于 References 匹配
- ``token``：``reply+<token>@domain`` 兜底路由（借鉴 mailin 的地址别名思路）

2026-11-14 改动：References 匹配从「``message_id_index LIKE '%id%'``」改为
``message_ids @> '["id"]'``（jsonb 包含），配合 GIN 索引，不再每条回复都全表扫。
``message_id_index`` 列保留（继续写入），只是不再作为查询条件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import JSONBType, TimestampMixin, UTCDateTime


class ThreadKind:
    RESUME_PACKAGE = "resume_package"        # 简历包（主管可直接在原邮件上回复名单）
    INTERVIEW_NOTICE = "interview_notice"    # 面试通知（候选人回复改期/放弃）
    INTERVIEW_UPDATE = "interview_update"    # 改期确认
    DECLINE_ACK = "decline_ack"              # 放弃确认


class EmailThread(TimestampMixin, Base):
    __tablename__ = "email_threads"
    __table_args__ = (
        Index("ix_email_threads_token", "token", unique=True),
        Index("ix_email_threads_root_message_id", "root_message_id"),
        Index("ix_email_threads_counterpart_email", "counterpart_email"),
        # 按候选人找线程（改期确认要挂回原会话）
        Index("ix_email_threads_candidate_kind", "candidate_id", "kind"),
        # 按主管找线程 + 外键 ON DELETE SET NULL 的定位列
        Index("ix_email_threads_supervisor_id", "supervisor_id"),
        # References 匹配：jsonb 包含查询 @> 走 GIN，替代原来的 LIKE '%...%' 全表扫
        Index(
            "ix_email_threads_message_ids_gin",
            "message_ids",
            postgresql_using="gin",
            postgresql_ops={"message_ids": "jsonb_path_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default=ThreadKind.INTERVIEW_NOTICE)

    root_message_id: Mapped[str] = mapped_column(String(255))
    #: 审计用：本线程全部 Message-ID（同时是 References 匹配的查询列）
    message_ids: Mapped[list[str]] = mapped_column(JSONBType, default=list)
    #: 历史兼容：空格拼接的同一批 Message-ID，旧版用它做 LIKE 匹配，现已不参与查询
    message_id_index: Mapped[str] = mapped_column(String(1024), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")

    reply_address: Mapped[str] = mapped_column(String(255), default="")
    counterpart_email: Mapped[str] = mapped_column(String(255), default="")

    candidate_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    supervisor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("supervisors.id", ondelete="SET NULL"), nullable=True
    )

    message_count: Mapped[int] = mapped_column(Integer, default=1)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EmailThread {self.kind} {self.token}>"
