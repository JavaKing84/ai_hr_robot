"""部门主管：飞书定向推送 + 邮箱副本（支持“在原邮件上回复”这条路径）。"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import TimestampMixin


class Supervisor(TimestampMixin, Base):
    __tablename__ = "supervisors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(32), default="")
    department: Mapped[str] = mapped_column(String(64), default="", index=True)
    feishu_open_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), default="")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Supervisor {self.name} {self.department}>"
