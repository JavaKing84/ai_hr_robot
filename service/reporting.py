"""运营视图：把 candidates / messages 里的留痕聚合成 HR 看得懂的两种数字。

只读，不写库、不改状态、不发任何外发消息——产品边界（不做 Web 后台、不做报表系统）
不受影响：这里给的是「一组数字」，用在哪（飞书日报、curl 自查）由调用方决定。

两个口径，别混着看：

1. **stage_snapshot（按当前状态分布）**——"现在每个人停在哪一步"。
   每个候选人只计一次，落在它当前的状态桶里。回答"手上还压着多少人"。
2. **daily（按天流水）**——"那天收了多少、其中过了多少、淘汰多少"。
   同一个候选人可能在不同天分别计入 received 与 rejected，这是流水不是存量。

``total`` 与 ``daily`` 的 ``received`` 都排除 ``is_duplicate`` 的重复投递，
否则同一个人的第 3 次投递会让"今天收了多少份"虚高。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.timeparse import now_utc_naive
from model.candidate import Candidate, CandidateStatus
from model.event import Event
from model.message import Channel, Direction, Message

#: 状态 → 阶段名。一个候选人只落一个阶段（当前状态），用于 stage_snapshot。
_STAGE_OF: dict[str, str] = {
    CandidateStatus.RECEIVED: "received",
    CandidateStatus.PARSED: "received",
    CandidateStatus.SCREENED_PASSED: "screened_passed",
    CandidateStatus.SCREENED_REJECTED: "screened_rejected",
    CandidateStatus.DISPATCHED: "dispatched",
    CandidateStatus.SUPERVISOR_FEEDBACK: "supervisor_feedback",
    CandidateStatus.INTERVIEW_PENDING_CONFIRM: "interview_pending_confirm",
    CandidateStatus.INTERVIEW_NOTIFIED: "interview_notified",
    CandidateStatus.RESCHEDULE_PENDING: "reschedule_pending",
    CandidateStatus.DECLINED: "declined",
    CandidateStatus.COMPLETED: "completed",
}

#: 视为「筛选通过」的终态集合（daily.passed 用）：只要没被淘汰就算通过
_PASSED_STATUSES = tuple(
    status for status in _STAGE_OF if status != CandidateStatus.SCREENED_REJECTED
)


@dataclass
class FunnelSnapshot:
    since: datetime
    until: datetime
    stage_snapshot: dict[str, int] = field(default_factory=dict)
    by_position: list[dict[str, object]] = field(default_factory=list)
    outbound: dict[str, int] = field(default_factory=dict)
    pending_approval: int = 0
    duplicate_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "stage_snapshot": self.stage_snapshot,
            "by_position": self.by_position,
            "outbound": self.outbound,
            "pending_approval": self.pending_approval,
            "duplicates_in_window": self.duplicate_count,
        }


class ReportingService:
    """只读聚合。所有条件都落在这几张表的索引列上（status / created_at / channel）。"""

    def __init__(self, session: AsyncSession, *, timezone: str = "Asia/Shanghai") -> None:
        self.session = session
        self.timezone = timezone

    async def funnel(self, *, days: int = 7) -> FunnelSnapshot:
        days = max(1, min(days, 90))
        until = now_utc_naive()
        since = until - timedelta(days=days)

        rows = await self.session.execute(
            select(Candidate.status, Candidate.position_code, func.count())
            .where(Candidate.created_at >= since, Candidate.is_duplicate.is_(False))
            .group_by(Candidate.status, Candidate.position_code)
        )
        snapshot: dict[str, int] = {}
        by_position: dict[str, dict[str, int]] = {}
        for status, position_code, count in rows.all():
            stage = _STAGE_OF.get(status, status or "unknown")
            snapshot[stage] = snapshot.get(stage, 0) + int(count)
            bucket = by_position.setdefault(str(position_code or "未归位"), {})
            bucket[stage] = bucket.get(stage, 0) + int(count)

        duplicates = await self.session.execute(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.created_at >= since, Candidate.is_duplicate.is_(True))
        )
        return FunnelSnapshot(
            since=since,
            until=until,
            stage_snapshot=snapshot,
            by_position=[
                {"position_code": code, **values}
                for code, values in sorted(by_position.items(), key=lambda item: -sum(item[1].values()))
            ],
            outbound=await self._outbound_counts(since),
            pending_approval=await self._pending_approval_count(),
            duplicate_count=int(duplicates.scalar_one()),
        )

    async def daily_counts(self, *, days: int = 7) -> list[dict[str, object]]:
        """按天流水：收（去重后）/ 通过 / 淘汰。"""
        days = max(1, min(days, 90))
        since = now_utc_naive() - timedelta(days=days)
        day = func.date(func.timezone(self.timezone, Candidate.created_at))
        rows = await self.session.execute(
            select(
                day.label("day"),
                func.count().label("received"),
                func.count().filter(Candidate.status != CandidateStatus.SCREENED_REJECTED).label("passed"),
                func.count().filter(Candidate.status == CandidateStatus.SCREENED_REJECTED).label("rejected"),
            )
            .where(Candidate.created_at >= since, Candidate.is_duplicate.is_(False))
            .group_by("day")
            .order_by("day")
        )
        return [
            {
                "day": _day_label(row.day),
                "received": int(row.received),
                "passed": int(row.passed),
                "rejected": int(row.rejected),
            }
            for row in rows.all()
        ]

    async def _outbound_counts(self, since: datetime) -> dict[str, int]:
        """最近一段时间各渠道的出站成功/失败数（失败数是运维最该先看的指标）。"""
        rows = await self.session.execute(
            select(Message.channel, Message.status, func.count())
            .where(Message.direction == Direction.OUTBOUND, Message.created_at >= since)
            .group_by(Message.channel, Message.status)
        )
        result: dict[str, int] = {}
        for channel, status, count in rows.all():
            key = f"{channel}.{status}"
            result[key] = result.get(key, 0) + int(count)
        return result

    async def _pending_approval_count(self) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.channel == Channel.EMAIL,
                Message.direction == Direction.OUTBOUND,
                Message.status == "pending_approval",
            )
        )
        return int(result.scalar_one())

    async def events_count(self, *, days: int = 1) -> int:
        since = now_utc_naive() - timedelta(days=max(1, days))
        result = await self.session.execute(
            select(func.count()).select_from(Event).where(Event.occurred_at >= since)
        )
        return int(result.scalar_one())


def _day_label(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


__all__ = ["FunnelSnapshot", "ReportingService"]
