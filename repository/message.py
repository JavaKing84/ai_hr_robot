"""消息留痕数据访问（飞书 / 邮件收发都用这张表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.threading import normalize_message_id
from model.message import Channel, Direction, Message
from repository.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def find_sent_by_message_id(self, message_id: str) -> Message | None:
        """按 Message-ID 找「我们自己发出去的邮件」。

        用途：识别自投回流的邮件。演示环境里主管邮箱就是 HR 邮箱（``18786112948@163.com``），
        简历包发出去会回到同一个收件箱，如果照常处理，它会被当成「主管说了句话」，
        每次发包都触发一次自问自答，还会重复登记一条线程（2026-11-14 真机实测到）。

        两种写法都试：发信按 ``<x@y>`` 生成并留痕，收信解析会把尖括号剥掉。
        """
        value = normalize_message_id(message_id)
        if not value:
            return None
        result = await self.session.execute(
            select(Message)
            .where(
                Message.external_id.in_((value, f"<{value}>")),
                Message.direction == Direction.OUTBOUND,
                Message.status.in_(("sent", "mocked")),
            )
            .limit(1)
        )
        return result.scalars().first()

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

    async def seen_inbound(self, channel: str, external_id: str) -> bool:
        """这条入站消息是否已经处理过。

        飞书回调在 3 秒内没拿到 200 就会重投，IMAP 侧也可能因为水位丢失重拉历史邮件。
        重复处理最坏的后果不是多一条留痕，而是**再给主管推一次改期请示、
        或者重复回复候选人**。所以在入口处按 (channel, external_id) 判重。
        """
        if not external_id:
            return False
        result = await self.session.execute(
            select(Message.id)
            .where(
                Message.channel == channel,
                Message.direction == Direction.INBOUND,
                Message.external_id == external_id,
            )
            .limit(1)
        )
        return result.scalars().first() is not None

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
