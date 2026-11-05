"""事件留痕：每一次状态变化 / 消息收发 / 筛选结果都追加一条（产品文档 `events`）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import TimestampMixin, utcnow_naive


class Actor:
    ROBOT = "robot"
    SUPERVISOR = "supervisor"
    CANDIDATE = "candidate"
    HR = "hr"
    SYSTEM = "system"


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(32), default=Actor.ROBOT)
    object_type: Mapped[str] = mapped_column(String(32), default="candidate")
    object_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)

    from_state: Mapped[str] = mapped_column(String(32), default="")
    to_state: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Event {self.action} {self.object_type}:{self.object_id}>"
