"""ORM 模型导出：``import model`` 即完成全部表的注册（建表 / 迁移依赖此行为）。"""

from model.candidate import Candidate, CandidateStatus
from model.email_thread import EmailThread, ThreadKind
from model.event import Actor, Event
from model.message import Channel, Direction, Message
from model.position import Position
from model.supervisor import Supervisor

__all__ = [
    "Candidate",
    "CandidateStatus",
    "EmailThread",
    "ThreadKind",
    "Actor",
    "Event",
    "Channel",
    "Direction",
    "Message",
    "Position",
    "Supervisor",
]
