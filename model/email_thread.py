"""邮件线程：持久化原始 Message-ID，后续发送带 In-Reply-To / References。

设计参考 Sentry 的 ``GroupEmailThread``（只借鉴模型设计，不复制代码）：
- ``root_message_id``：线程根，回复邮件靠它挂回业务实体
- ``message_ids``：本线程全部 Message-ID，便于 References 匹配
- ``token``：``reply+<token>@domain`` 兜底路由（借鉴 mailin 的地址别名思路）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import TimestampMixin


class ThreadKind:
    RESUME_PACKAGE = "resume_package"        # 简历包（主管可直接在原邮件上回复名单）
    INTERVIEW_NOTICE = "interview_notice"    # 面试通知（候选人回复改期/放弃）
    INTERVIEW_UPDATE = "interview_update"    # 改期确认
    DECLINE_ACK = "decline_ack"              # 放弃确认


class EmailThread(TimestampMixin, Base):
    __tablename__ = "email_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default=ThreadKind.INTERVIEW_NOTICE, index=True)

    root_message_id: Mapped[str] = mapped_column(String(255), index=True)
    #: 审计用：本线程全部 Message-ID
    message_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: 匹配用：空格拼接的同一批 Message-ID。SQLite 对 JSON 列的包含查询不友好，
    #: 用一列扁平字符串做 LIKE 匹配，语义等价且走得上索引。
    message_id_index: Mapped[str] = mapped_column(String(1024), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")

    reply_address: Mapped[str] = mapped_column(String(255), default="")
    counterpart_email: Mapped[str] = mapped_column(String(255), default="", index=True)

    candidate_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    supervisor_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, default=1)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EmailThread {self.kind} {self.token}>"
