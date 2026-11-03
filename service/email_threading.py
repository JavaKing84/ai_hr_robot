"""邮件线程匹配：解析 In-Reply-To / References，匹配失败时逐级兜底。

匹配顺序（借鉴 Zammad 的「头部优先 + 逐级兜底」策略，只借鉴逻辑）：
1. ``In-Reply-To`` / ``References`` → 命中的 EmailThread
2. 收件地址里的 ``reply+<token>@domain`` 别名（借鉴 mailin 的 token 路由思路）
3. 发件人是某位主管 → 其最近的简历包线程
4. 发件人是某位候选人 → 其面试通知线程
5. 都匹配不上 → unknown，交给人工/日志
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from client.mailbox import InboundEmail
from config.settings import Settings
from core.logging import get_logger, log_extra
from model.candidate import Candidate
from model.email_thread import EmailThread, ThreadKind
from model.position import Position
from model.supervisor import Supervisor
from repository.email_thread import EmailThreadRepository

logger = get_logger(__name__)

_REPLY_TOKEN_RE = re.compile(r"reply\+([A-Za-z0-9._-]+)@", re.IGNORECASE)


@dataclass
class ThreadMatch:
    role: str = "unknown"          # candidate / supervisor / unknown
    thread: EmailThread | None = None
    candidate: Candidate | None = None
    position: Position | None = None
    supervisor: Supervisor | None = None
    reason: str = ""


class EmailThreadService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.threads = EmailThreadRepository(session)

    async def match(self, email: InboundEmail) -> ThreadMatch:
        thread, reason = await self._locate_thread(email)
        if thread is not None:
            if thread.kind == ThreadKind.RESUME_PACKAGE:
                position = await self.session.get(Position, thread.position_id) if thread.position_id else None
                supervisor = None
                if position is not None:
                    supervisor = (
                        await self.session.execute(
                            select(Supervisor).where(
                                Supervisor.feishu_open_id == position.supervisor_open_id
                            )
                        )
                    ).scalars().first()
                return ThreadMatch(
                    role="supervisor",
                    thread=thread,
                    position=position,
                    supervisor=supervisor,
                    reason=reason,
                )

            candidate = await self.session.get(Candidate, thread.candidate_id) if thread.candidate_id else None
            position = await self.session.get(Position, thread.position_id) if thread.position_id else None
            return ThreadMatch(
                role="candidate",
                thread=thread,
                candidate=candidate,
                position=position,
                reason=reason,
            )

        # 兜底 3：发件人是主管
        supervisor = await self._find_supervisor(email.from_address)
        if supervisor is not None:
            position = (
                await self.session.execute(
                    select(Position)
                    .where(Position.supervisor_open_id == supervisor.feishu_open_id, Position.active.is_(True))
                    .order_by(Position.id)
                    .limit(1)
                )
            ).scalars().first()
            return ThreadMatch(
                role="supervisor",
                position=position,
                supervisor=supervisor,
                reason="按发件人邮箱识别为主管",
            )

        # 兜底 4：发件人是候选人
        candidate = (
            await self.session.execute(
                select(Candidate).where(Candidate.email == email.from_address.lower()).order_by(Candidate.id.desc()).limit(1)
            )
        ).scalars().first()
        if candidate is not None:
            position = await self.session.get(Position, candidate.position_id) if candidate.position_id else None
            thread = await self.threads.find_for_candidate(candidate.id)
            return ThreadMatch(
                role="candidate",
                thread=thread,
                candidate=candidate,
                position=position,
                reason="按发件人邮箱识别为候选人",
            )

        logger.info("thread.unmatched", extra=log_extra(sender=email.from_address, subject=email.subject))
        return ThreadMatch(role="unknown", reason="没有可匹配的线程/主管/候选人")

    async def _locate_thread(self, email: InboundEmail) -> tuple[EmailThread | None, str]:
        header_ids = [email.in_reply_to, *email.references]
        thread = await self.threads.find_by_any_message_id([mid for mid in header_ids if mid])
        if thread is not None:
            return thread, "按 In-Reply-To/References 匹配"

        token = extract_reply_token(email.to_addresses + email.cc_addresses)
        if token:
            thread = await self.threads.find_by_token(token)
            if thread is not None:
                return thread, "按 reply+token 别名匹配"

        return None, "头部与 token 均未命中"

    async def _find_supervisor(self, address: str) -> Supervisor | None:
        if not address:
            return None
        result = await self.session.execute(
            select(Supervisor).where(Supervisor.email.ilike(address.strip().lower()))
        )
        return result.scalars().first()

    async def attach_thread(
        self,
        *,
        email: InboundEmail,
        thread: EmailThread | None,
        candidate_id: int | None = None,
        position_id: int | None = None,
        kind: str = ThreadKind.INTERVIEW_NOTICE,
    ) -> EmailThread:
        """入站邮件没有命中线程时，为它补一条线程记录（保证后续回复能挂上）。"""
        if thread is not None:
            await self.threads.append_message_id(thread, email.message_id, at=email.received_at)
            return thread
        return await self.threads.create(
            token=f"in-{email.message_id.strip('<>')[:24]}",
            root_message_id=email.message_id,
            kind=kind,
            subject=email.subject,
            counterpart_email=email.from_address,
            candidate_id=candidate_id,
            position_id=position_id,
        )


def extract_reply_token(addresses: list[str]) -> str:
    for address in addresses or []:
        match = _REPLY_TOKEN_RE.search(address or "")
        if match:
            return match.group(1)
    return ""


__all__ = ["EmailThreadService", "ThreadMatch", "extract_reply_token"]
