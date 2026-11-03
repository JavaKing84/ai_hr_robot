"""ORM 公共部分：时间戳混入 + naive UTC 时间工具。

约定：所有 ``DateTime`` 列存 **naive UTC**。SQLite 不保留时区信息，统一存 UTC
可以避免 naive/aware 混用导致的比较报错；对外展示统一走 ``core.timeparse``。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False
    )
