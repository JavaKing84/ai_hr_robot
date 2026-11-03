"""消息留痕：飞书 / 邮件的收与发都落这张表（产品文档 `messages`）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import TimestampMixin


class Channel:
    FEISHU = "feishu"
    EMAIL = "email"


class Direction:
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)

    sender: Mapped[str] = mapped_column(String(255), default="")
    recipient: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")

    candidate_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    supervisor_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    external_id: Mapped[str] = mapped_column(String(255), default="", index=True)  # 飞书 message_id / 邮件 Message-ID
    thread_key: Mapped[str] = mapped_column(String(255), default="", index=True)

    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="sent")  # sent / received / failed / mocked
    error: Mapped[str] = mapped_column(Text, default="")

    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self.channel}/{self.direction} {self.id}>"
