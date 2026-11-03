"""消息留痕数据访问（飞书 / 邮件收发都用这张表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model.message import Channel, Direction, Message
from repository.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def list_for_candidate(self, candidate_id: int) -> list[Message]:
        result = await self.session.execute(
            select(Message).where(Message.candidate_id == candidate_id).order_by(Message.id)
        )
        return list(result.scalars().all())

    async def list_by_thread(self, thread_key: str) -> list[Message]:
        result = await self.session.execute(
            select(Message).where(Message.thread_key == thread_key).order_by(Message.id)
        )
        return list(result.scalars().all())

    async def find_by_external_id(self, external_id: str) -> Message | None:
        if not external_id:
            return None
        result = await self.session.execute(
            select(Message).where(Message.external_id == external_id).limit(1)
        )
        return result.scalars().first()

    async def list_recent(
        self, *, channel: str | None = None, direction: str | None = None, limit: int = 50
    ) -> list[Message]:
        stmt = select(Message)
        if channel:
            stmt = stmt.where(Message.channel == channel)
        if direction:
            stmt = stmt.where(Message.direction == direction)
        result = await self.session.execute(stmt.order_by(Message.id.desc()).limit(limit))
        return list(result.scalars().all())

    async def list_outbound_feishu(self, limit: int = 50) -> list[Message]:
        return await self.list_recent(channel=Channel.FEISHU, direction=Direction.OUTBOUND, limit=limit)


def build_message_repository(session: AsyncSession) -> MessageRepository:
    return MessageRepository(session)
