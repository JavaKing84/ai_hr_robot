"""职位（含主管映射与筛选规则的运行期快照）。

配置来源是 ``config/positions.yaml``；每轮任务开始前由
``repository/position.py::PositionRepository.sync_from_config`` 同步进来，
因此 HR 改 YAML 后“下一轮即生效”，同时数据库里保留了规则版本供追溯。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import JSONBType, TimestampMixin


class Position(TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_code", "code", unique=True),
        # 主管认人 + 只取在招：两条条件一起走索引，找「我名下有哪些在招职位」不必回表扫
        Index(
            "ix_positions_supervisor_active",
            "supervisor_open_id",
            "active",
            postgresql_where=text("active IS TRUE"),
        ),
        Index("ix_positions_department", "department"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    department: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    headcount: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list[str]] = mapped_column(JSONBType, default=list)

    # 筛选规则快照
    threshold: Mapped[float] = mapped_column(Float, default=60.0)
    rule_version: Mapped[str] = mapped_column(String(32), default="v1")
    screening: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)

    # 主管映射（飞书定向 + 邮箱副本）
    supervisor_name: Mapped[str] = mapped_column(String(64), default="")
    supervisor_title: Mapped[str] = mapped_column(String(32), default="")
    supervisor_open_id: Mapped[str] = mapped_column(String(128), default="")
    supervisor_email: Mapped[str] = mapped_column(String(255), default="")

    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Position {self.code} {self.title}>"
