"""部门主管：飞书定向推送 + 邮箱副本（支持“在原邮件上回复”这条路径）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.base import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免 model 之间循环导入
    from model.position import Position


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


def supervisor_from_position(position: "Position") -> "Supervisor | None":
    """用职位上冗余的主管信息拼一个「临时主管」对象（**不写库**）。

    为什么需要它：``supervisors.feishu_open_id`` 是唯一索引，而现实里多个部门主管
    常常共用同一个飞书号（演示环境里三位主管的 ``feishu_open_id`` 都是 HR 本人），
    同步时会被挤成一行、只剩最后一个部门的主管。职位表反而完整保存了每个岗位的
    主管姓名/邮箱/飞书号，所以「邮件按发件地址认人」必须回落到职位配置，
    否则只会在 supervisors 表里查到一个人，其他主管的回信被判成陌生人丢掉。

    返回的对象只用于读取 ``name/title/email/feishu_open_id``，不加入会话；
    调用方不要对它执行 ``session.add`` / ``delete``。
    """
    if position is None:  # pragma: no cover - 防御
        return None
    if not position.supervisor_email and not position.supervisor_open_id:
        return None
    return Supervisor(
        name=position.supervisor_name or "主管",
        title=position.supervisor_title or "",
        department=position.department or "",
        feishu_open_id=position.supervisor_open_id or "",
        email=position.supervisor_email or "",
    )


__all__ = ["Supervisor", "supervisor_from_position"]
