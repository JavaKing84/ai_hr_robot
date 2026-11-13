"""消息留痕：飞书 / 邮件的收与发都落这张表（产品文档 `messages`）。

索引与唯一约束（2026-11-14）：
- ``ux_messages_channel_external``：``(channel, external_id)`` 部分唯一索引，只约束
  ``external_id <> '' AND status IN ('received','sent','mocked')`` 的行。
  这样「同一条飞书/邮件消息被重复回调」在数据库层就写不进第二条，
  同时不破坏既有设计：失败重投会留 ``failed`` 行、人工确认闸门会留
  ``pending_approval`` 行、确认后原草稿转 ``superseded``——这三类都不在约束范围内。
- ``(channel, direction, id)``：最近消息列表（``list_recent``）按时间倒序取。
- ``(channel, direction, status)`` 部分索引：补发入口只扫 ``failed`` /
  ``pending_approval`` 这两种状态，索引体积比全表小一个量级。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import JSONBType, TimestampMixin, UTCDateTime


class Channel:
    FEISHU = "feishu"
    EMAIL = "email"


class Direction:
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "ux_messages_channel_external",
            "channel",
            "external_id",
            unique=True,
            postgresql_where=text(
                "external_id <> '' AND status IN ('received', 'sent', 'mocked')"
            ),
        ),
        Index("ix_messages_channel_direction_id", "channel", "direction", "id"),
        Index("ix_messages_candidate_id", "candidate_id"),
        Index("ix_messages_thread_key", "thread_key"),
        # 幂等判重的两条热路径（find_delivered / seen_inbound）都只按 external_id 定位。
        # 唯独靠上面那条**部分**唯一索引是不够的：它的谓词里有 status，
        # 而查询并没有约束 status，PostgreSQL 无法证明查询行落在索引里，
        # 只能退化成全表扫（实测 6 万行 7.5ms/次，每条入站消息都要付一次）。
        # 这条单列索引不带谓词语义，供查询使用；唯一性仍由 ux_messages_channel_external 保证。
        Index(
            "ix_messages_external_id",
            "external_id",
            postgresql_where=text("external_id <> ''"),
        ),
        Index(
            "ix_messages_retry",
            "channel",
            "direction",
            "status",
            postgresql_where=text("status IN ('failed', 'pending_approval')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(16))

    sender: Mapped[str] = mapped_column(String(255), default="")
    recipient: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")

    candidate_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    supervisor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("supervisors.id", ondelete="SET NULL"), nullable=True
    )

    external_id: Mapped[str] = mapped_column(String(255), default="")  # 飞书 message_id / 邮件 Message-ID
    thread_key: Mapped[str] = mapped_column(String(255), default="")

    parsed: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="sent")  # sent / received / failed / mocked
    error: Mapped[str] = mapped_column(Text, default="")

    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self.channel}/{self.direction} {self.id}>"
