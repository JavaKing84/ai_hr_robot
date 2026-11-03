"""邮件线程数据访问：Message-ID 匹配、token 兜底匹配。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from model.base import utcnow_naive
from model.email_thread import EmailThread, ThreadKind
from repository.base import BaseRepository


class EmailThreadRepository(BaseRepository[EmailThread]):
    model = EmailThread

    async def create(
        self,
        *,
        token: str,
        root_message_id: str,
        kind: str = ThreadKind.INTERVIEW_NOTICE,
        subject: str = "",
        reply_address: str = "",
        counterpart_email: str = "",
        candidate_id: int | None = None,
        position_id: int | None = None,
        supervisor_id: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> EmailThread:
        thread = EmailThread(
            token=token,
            kind=kind,
            root_message_id=root_message_id,
            message_ids=[root_message_id],
            message_id_index=root_message_id,
            subject=subject,
            reply_address=reply_address,
            counterpart_email=counterpart_email.lower(),
            candidate_id=candidate_id,
            position_id=position_id,
            supervisor_id=supervisor_id,
            message_count=1,
            last_message_at=utcnow_naive(),
            meta=meta or {},
        )
        return await self.add(thread)

    async def find_by_token(self, token: str) -> EmailThread | None:
        if not token:
            return None
        result = await self.session.execute(
            select(EmailThread).where(EmailThread.token == token).limit(1)
        )
        return result.scalars().first()

    async def find_by_root_message_id(self, message_id: str) -> EmailThread | None:
        if not message_id:
            return None
        result = await self.session.execute(
            select(EmailThread).where(EmailThread.root_message_id == message_id).limit(1)
        )
        return result.scalars().first()

    async def find_by_any_message_id(self, message_ids: Sequence[str]) -> EmailThread | None:
        """按 In-Reply-To / References 里的任意 Message-ID 命中所属线程。"""
        cleaned = [mid for mid in message_ids if mid]
        if not cleaned:
            return None
        conditions = [EmailThread.root_message_id.in_(cleaned)]
        conditions += [EmailThread.message_id_index.like(f"%{mid}%") for mid in cleaned]
        result = await self.session.execute(
            select(EmailThread).where(or_(*conditions)).order_by(EmailThread.id.desc()).limit(1)
        )
        return result.scalars().first()

    async def find_for_candidate(
        self, candidate_id: int, kind: str | None = None
    ) -> EmailThread | None:
        stmt = select(EmailThread).where(EmailThread.candidate_id == candidate_id)
        if kind:
            stmt = stmt.where(EmailThread.kind == kind)
        result = await self.session.execute(stmt.order_by(EmailThread.id.desc()).limit(1))
        return result.scalars().first()

    async def find_for_supervisor(self, supervisor_id: int, kind: str | None = None) -> EmailThread | None:
        stmt = select(EmailThread).where(EmailThread.supervisor_id == supervisor_id)
        if kind:
            stmt = stmt.where(EmailThread.kind == kind)
        result = await self.session.execute(stmt.order_by(EmailThread.id.desc()).limit(1))
        return result.scalars().first()

    async def append_message_id(self, thread: EmailThread, message_id: str, *, at: datetime | None = None) -> None:
        """记录本次发出的新 Message-ID（下次回复的 In-Reply-To 会指向它）。"""
        ids = list(thread.message_ids or [])
        ids.append(message_id)
        thread.message_ids = ids
        thread.message_id_index = " ".join(ids)[:1024]
        thread.message_count = (thread.message_count or 1) + 1
        thread.last_message_at = at or utcnow_naive()
        await self.session.flush()


def build_email_thread_repository(session: AsyncSession) -> EmailThreadRepository:
    return EmailThreadRepository(session)
