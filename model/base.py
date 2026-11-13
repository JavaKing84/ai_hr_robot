"""ORM 公共部分：时间戳混入、PG 专用列类型与 naive UTC 时间工具。

约定（2026-11-14 收紧）：**数据库列用 ``timestamptz`` 存真实时间点，应用层仍只处理
naive UTC**。两者之间由 :class:`UTCDateTime` 负责转换：

- 写入：naive 值按 UTC 解释，aware 值先转 UTC，统一以带时区值落库；
- 读出：统一去掉 tzinfo 变回 naive UTC 交给业务代码。

好处是「跨时区不会算错」（数据库知道自己存的是哪个瞬间），而业务代码里
``datetime`` 比较、``isoformat()``、事件 detail 里的字符串格式全都不变，
不需要改动任何既有调用点。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UTCDateTime(TypeDecorator):
    """``timestamptz`` 列 ↔ 应用层 naive UTC。

    ``impl = DateTime(timezone=True)``：DDL 生成 ``TIMESTAMP WITH TIME ZONE``，
    Alembic / ``create_all`` 都按它建表，测试库与生产库结构一致。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # 业务层约定 naive = UTC，这里补上时区标记而不是改时间数值
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)


#: JSON 列统一用 jsonb：可建 GIN 索引、支持 ``?`` / ``@>`` 运算符，
#: 按技能、分数明细检索不必全表扫。项目只支持 PostgreSQL，不需要回退方言。
JSONBType = JSONB


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False
    )


__all__ = ["JSONBType", "TimestampMixin", "UTCDateTime", "utcnow_naive"]
