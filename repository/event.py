"""事件留痕数据访问。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model.event import Actor, Event
from model.base import utcnow_naive
from repository.base import BaseRepository


class EventRepository(BaseRepository[Event]):
    model = Event

    async def record(
        self,
        action: str,
        *,
        actor: str = Actor.ROBOT,
        object_type: str = "candidate",
        object_id: int | None = None,
        from_state: str = "",
        to_state: str = "",
        detail: dict[str, Any] | None = None,
        note: str = "",
        occurred_at: datetime | None = None,
    ) -> Event:
        event = Event(
            action=action,
            actor=actor,
            object_type=object_type,
            object_id=object_id,
            from_state=from_state,
            to_state=to_state,
            detail=detail or {},
            note=note,
            occurred_at=occurred_at or utcnow_naive(),
        )
        return await self.add(event)

    async def list_for_object(self, object_type: str, object_id: int) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(Event.object_type == object_type, Event.object_id == object_id)
            .order_by(Event.id)
        )
        return list(result.scalars().all())

    async def list_recent(self, *, limit: int = 100, action: str | None = None) -> list[Event]:
        stmt = select(Event)
        if action:
            stmt = stmt.where(Event.action == action)
        result = await self.session.execute(stmt.order_by(Event.id.desc()).limit(limit))
        return list(result.scalars().all())

    async def list_since(self, since: datetime, limit: int = 500) -> list[Event]:
        result = await self.session.execute(
            select(Event).where(Event.occurred_at >= since).order_by(Event.id).limit(limit)
        )
        return list(result.scalars().all())


def build_event_repository(session: AsyncSession) -> EventRepository:
    return EventRepository(session)
