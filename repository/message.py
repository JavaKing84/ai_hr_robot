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

    async def find_delivered(self, external_id: str) -> Message | None:
        """幂等防重（§4-A.1a）：同一 external_id 是否已有成功发出的出站记录。

        只认 ``sent`` / ``mocked``（成功）；``failed`` 记录不拦截，允许按原键补发。
        """
        if not external_id:
            return None
        result = await self.session.execute(
            select(Message)
            .where(
                Message.external_id == external_id,
                Message.direction == Direction.OUTBOUND,
                Message.status.in_(("sent", "mocked")),
            )
            .limit(1)
        )
        return result.scalars().first()

    async def find_delivered_by_content(
        self, *, channel: str, recipient: str, body: str
    ) -> Message | None:
        """内容级幂等查重：飞书 message_id 由服务端生成、发送前不存在，
        因此对飞书出站按「收件人 + 正文精确匹配」判重（应用层查表，§4-A.1a 边界内）。"""
        if not recipient or not body:
            return None
        result = await self.session.execute(
            select(Message)
            .where(
                Message.channel == channel,
                Message.direction == Direction.OUTBOUND,
                Message.recipient == recipient,
                Message.body == body,
                Message.status.in_(("sent", "mocked")),
            )
            .limit(1)
        )
        return result.scalars().first()

    async def list_failed_outbound(self, *, channel: str) -> list[Message]:
        """失败的出站记录（补发入口的数据源，按 id 升序）。"""
        result = await self.session.execute(
            select(Message)
            .where(
                Message.channel == channel,
                Message.direction == Direction.OUTBOUND,
                Message.status == "failed",
            )
            .order_by(Message.id)
        )
        return list(result.scalars().all())

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
