"""邮件线程数据访问：Message-ID 匹配、token 兜底匹配。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from model.base import utcnow_naive
from model.email_thread import EmailThread, ThreadKind
from repository.base import BaseRepository
from core.threading import normalize_message_id


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
        # Message-ID 统一去掉尖括号再存：发信时我们是按 ``<xxx@domain>`` 生成的，
        # 而收信解析（``parse_email_bytes``）会把尖括号剥掉。两边格式不一致时，
        # 主管/候选人的回信永远匹配不上 In-Reply-To，只能退化到发件人兜底——
        # 2026-11-14 补测试时实测到这个问题，此前「原邮件回复」实际没被这条路径命中过。
        root = normalize_message_id(root_message_id) or root_message_id
        thread = EmailThread(
            token=token,
            kind=kind,
            root_message_id=root,
            message_ids=[root],
            message_id_index=root,
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
        value = normalize_message_id(message_id)
        if not value:
            return None
        result = await self.session.execute(
            select(EmailThread).where(EmailThread.root_message_id == value).limit(1)
        )
        return result.scalars().first()

    async def find_by_any_message_id(self, message_ids: Sequence[str]) -> EmailThread | None:
        """按 In-Reply-To / References 里的任意 Message-ID 命中所属线程。

        2026-11-14：改用 jsonb 包含查询（``message_ids @> '["<id>"]'``）配合 GIN 索引，
        不再用 ``LIKE '%<id>%'``——后者前导通配符用不上索引，每条回信都要全表扫一遍。
        查询前统一归一化（去尖括号），与 ``create()`` 的存储格式保持一致。
        """
        cleaned = [normalize_message_id(mid) for mid in message_ids if mid]
        cleaned = [mid for mid in cleaned if mid]
        if not cleaned:
            return None
        conditions = [EmailThread.root_message_id.in_(cleaned)]
        conditions += [EmailThread.message_ids.contains([mid]) for mid in cleaned]
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
        value = normalize_message_id(message_id) or message_id
        ids = list(thread.message_ids or [])
        if value in ids:
            return
        ids.append(value)
        thread.message_ids = ids
        thread.message_id_index = " ".join(ids)[:1024]
        thread.message_count = (thread.message_count or 1) + 1
        thread.last_message_at = at or utcnow_naive()
        await self.session.flush()


def build_email_thread_repository(session: AsyncSession) -> EmailThreadRepository:
    return EmailThreadRepository(session)
