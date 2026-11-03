"""候选人数据访问：去重查找、按职位/状态查询、分页检索。"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from model.candidate import Candidate, CandidateStatus
from repository.base import BaseRepository


class CandidateRepository(BaseRepository[Candidate]):
    model = Candidate

    async def find_duplicate(self, *, email: str = "", phone: str = "") -> Candidate | None:
        """按「邮箱 + 手机号」找首次记录：任一命中即视为重复投递。"""
        conditions = []
        if email:
            conditions.append(Candidate.email == email.strip().lower())
        if phone:
            conditions.append(Candidate.phone == phone.strip())
        if not conditions:
            return None
        result = await self.session.execute(
            select(Candidate)
            .where(or_(*conditions), Candidate.is_duplicate.is_(False))
            .order_by(Candidate.id)
            .limit(1)
        )
        return result.scalars().first()

    async def list_by_position(
        self, position_id: int, statuses: Sequence[str] | None = None
    ) -> list[Candidate]:
        stmt = select(Candidate).where(Candidate.position_id == position_id)
        if statuses:
            stmt = stmt.where(Candidate.status.in_(list(statuses)))
        stmt = stmt.order_by(Candidate.score.desc(), Candidate.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_cycle(self, cycle_id: int) -> list[Candidate]:
        result = await self.session.execute(
            select(Candidate).where(Candidate.cycle_id == cycle_id).order_by(Candidate.id)
        )
        return list(result.scalars().all())

    async def list_dispatched_for_positions(self, position_ids: Sequence[int]) -> list[Candidate]:
        """某一轮已经发给主管、等待其反馈的候选人。"""
        if not position_ids:
            return []
        result = await self.session.execute(
            select(Candidate)
            .where(
                Candidate.position_id.in_(list(position_ids)),
                Candidate.status.in_([CandidateStatus.DISPATCHED, CandidateStatus.SUPERVISOR_FEEDBACK]),
            )
            .order_by(Candidate.position_id, Candidate.score.desc())
        )
        return list(result.scalars().all())

    async def list_active_for_positions(
        self, position_ids: Sequence[int], statuses: Sequence[str]
    ) -> list[Candidate]:
        """按职位 + 状态取候选人（主管反馈/改期确认时用来找“在流程里”的人）。"""
        if not position_ids or not statuses:
            return []
        result = await self.session.execute(
            select(Candidate)
            .where(
                Candidate.position_id.in_(list(position_ids)),
                Candidate.status.in_(list(statuses)),
            )
            .order_by(Candidate.position_id, Candidate.score.desc())
        )
        return list(result.scalars().all())

    async def list_by_names(self, names: Sequence[str], position_ids: Sequence[int]) -> list[Candidate]:
        if not names or not position_ids:
            return []
        result = await self.session.execute(
            select(Candidate).where(
                Candidate.name.in_(list(names)), Candidate.position_id.in_(list(position_ids))
            )
        )
        return list(result.scalars().all())

    async def list_interview_scheduled(self) -> list[Candidate]:
        result = await self.session.execute(
            select(Candidate)
            .where(Candidate.interview_time.is_not(None))
            .order_by(Candidate.interview_time)
        )
        return list(result.scalars().all())

    def build_search(
        self,
        *,
        status: str | None = None,
        position_code: str | None = None,
        cycle_id: int | None = None,
        name: str | None = None,
    ) -> Select[tuple[Candidate]]:
        stmt = select(Candidate)
        if status:
            stmt = stmt.where(Candidate.status == status)
        if position_code:
            stmt = stmt.where(Candidate.position_code == position_code)
        if cycle_id is not None:
            stmt = stmt.where(Candidate.cycle_id == cycle_id)
        if name:
            stmt = stmt.where(Candidate.name.ilike(f"%{name}%"))
        return stmt.order_by(Candidate.id.desc())

    async def search(self, *, limit: int = 50, offset: int = 0, **filters) -> tuple[list[Candidate], int]:
        stmt = self.build_search(**filters)
        total = await self.count(stmt)
        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all()), total


def build_candidate_repository(session: AsyncSession) -> CandidateRepository:
    return CandidateRepository(session)
