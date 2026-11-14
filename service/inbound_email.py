"""入站邮件处理：一次轮询、三类分流。

分流规则（保证「收简历」与「处理回复」不会互相吞掉对方的邮件）：
0. 带简历附件 → 一律视为「新简历投递」，跳过按发件人认人的兜底（见下）
0.5 机器人自己发出的邮件回流 → 直接忽略（主管邮箱与 HR 邮箱相同时会自己发给自己）
1. 命中线程 / 发件人是候选人 → 候选人回信（改期、放弃、确认）
2. 命中线程 / 发件人是主管 → 主管反馈（走 ``service/feedback.py``，与飞书同一条代码路径）
3. 未命中且带简历附件（或正文像简历）→ 交给 ``service/ingestion.py`` 入库
4. 其他 → 记一条 inbound 消息 + 事件，忽略

规则 0 是 2026-11-12 补的：招聘平台所有通知邮件共用一个 noreply 发件人
（如 BOSS直聘 ``cv@notice.zhipin.com``），如果按发件人去认候选人，第一封之后
每一封新简历都会被当成同一位候选人的回信，附件根本不入库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from client.mailbox import InboundEmail, MailboxClient
from config.settings import Settings
from core.logging import get_logger, log_extra
from core.timeparse import format_interview_time, to_utc_naive
from model.candidate import Candidate, CandidateStatus
from model.event import Actor
from model.message import Channel, Direction, Message
from model.position import Position
from repository.event import EventRepository
from repository.message import MessageRepository
from schema.candidate import EmailPollResponse
from schema.config import MailboxConfig
from schema.message import InboundMessage
from service.email_threading import (
    RESUME_ATTACHMENT_SUFFIXES,
    SUPERVISOR_THREAD_KINDS,
    EmailThreadService,
    ThreadMatch,
    has_resume_attachment,
)
from service.extraction import ExtractionService
from service.feedback import FeedbackService
from service.ingestion import IngestionService
from service.notification import NotificationService

logger = get_logger(__name__)

# 与线程判定共用同一份后缀白名单，避免「这封算不算简历」在两层出现口径分歧。
_RESUME_SUFFIXES = RESUME_ATTACHMENT_SUFFIXES
_RESUME_HINTS = ("简历", "应聘", "求职", "教育背景", "工作经历", "项目经验")


@dataclass
class InboundOutcome:
    kind: str
    handled: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


class InboundEmailService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        mailbox: MailboxClient,
        threads: EmailThreadService,
        feedback: FeedbackService,
        ingestion: IngestionService,
        notification: NotificationService,
        extraction: ExtractionService,
        mailbox_config: MailboxConfig | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.mailbox = mailbox
        self.threads = threads
        self.feedback = feedback
        self.ingestion = ingestion
        self.notification = notification
        self.extraction = extraction
        self.mailbox_config = mailbox_config or MailboxConfig()
        self.events = EventRepository(session)
        self.messages = MessageRepository(session)

    # ------------------------------------------------------------------ 轮询
    async def poll_once(self, *, cycle_id: int | None = None) -> EmailPollResponse:
        response = EmailPollResponse()
        emails = await self.mailbox.poll_inbox()
        for email in emails:
            try:
                outcome = await self.handle_email(email, cycle_id=cycle_id)
            except Exception as exc:  # noqa: BLE001 - 单封邮件失败不影响后续
                logger.exception("inbound.failed", extra=log_extra(subject=email.subject))
                response.failed += 1
                response.errors.append(f"{email.subject or email.from_address}: {exc}")
                # 必须回滚：数据库类异常（典型是两个轮询同时跑撞上 messages 的唯一索引）
                # 会把会话标记成「需要回滚」，之后所有语句都报 PendingRollbackError——
                # 于是单封失败被放大成整轮失败，结尾的 commit 再抛一次，接口直接 500。
                # 2026-11-14 实测：11 封邮件全部入库成功，接口却回了 500，就是这个原因。
                try:
                    await self.session.rollback()
                except Exception:  # noqa: BLE001 - 回滚都失败就只能放弃这一轮，别再抛
                    logger.exception("inbound.rollback_failed", extra=log_extra(subject=email.subject))
                continue
            if outcome.handled:
                response.processed += 1
            else:
                response.ignored += 1
            response.details.append(
                {"kind": outcome.kind, "handled": outcome.handled, "from": email.from_address, **outcome.detail}
            )
        await self.session.commit()
        return response

    # ------------------------------------------------------------------ 分流
    async def handle_email(self, email: InboundEmail, *, cycle_id: int | None = None) -> InboundOutcome:
        # 幂等闸门：同一封邮件（同一 Message-ID）只处理一次。
        # IMAP 水位丢失、或多实例轮询交错时会出现「同一封信被处理两遍」，
        # 后果是重复给主管推送、重复补发邮件——这里在入口挡住。
        if await self.messages.seen_inbound(Channel.EMAIL, email.message_id):
            logger.info(
                "inbound.duplicate_skipped",
                extra=log_extra(message_id=email.message_id, subject=email.subject),
            )
            return InboundOutcome(
                kind="duplicate", handled=False, detail={"reason": "重复邮件（Message-ID 已处理）"}
            )

        # 自投回流：机器人自己发出的邮件又回到了收件箱。
        # 演示环境里主管邮箱就是 HR 邮箱（18786112948@163.com），简历包发出去会绕回同一个 INBOX；
        # 不挡的话它会被当成「主管说了句话」，每次发包都自问自答一次，还会重复登记一条线程。
        if await self.messages.find_sent_by_message_id(email.message_id) is not None:
            logger.info(
                "inbound.self_echo_skipped",
                extra=log_extra(message_id=email.message_id, subject=email.subject),
            )
            return InboundOutcome(
                kind="self_echo",
                handled=False,
                detail={"reason": "这是机器人自己发出的邮件回流，不再当作主管/候选人的消息"},
            )

        match = await self.threads.match(email)
        # 双保险：即使线程匹配命中了主管/候选人，带简历附件的邮件也优先入库。
        # 真正需要按「回信」处理的邮件几乎不会带简历附件，且会带 In-Reply-To。
        resume_attached = has_resume_attachment(email)
        # 例外：主管用「转发」寄回，或在回复里又附上了一份简历，回信同样带简历附件。
        # 「带附件=新投递」是防平台共用发件人吞掉新简历的（2026-11-12 的 bug），
        # 而挂在简历包线程上的回信不可能是候选人发来的——那条线程只由机器人发出。
        # 这里只放行这一种情况，其他带附件的邮件仍走原逻辑。
        supervisor_reply_on_thread = (
            match.role == "supervisor"
            and match.thread is not None
            and match.thread.kind in SUPERVISOR_THREAD_KINDS
            and bool(email.in_reply_to or email.references)
        )

        if match.role == "candidate" and match.candidate is not None and not resume_attached:
            return await self._handle_candidate_reply(email, match)

        if match.role == "supervisor" and (not resume_attached or supervisor_reply_on_thread):
            inbound = InboundMessage(
                channel="email",
                sender_id=email.from_address,
                sender_name=email.from_name,
                text=email.body_text,
                thread_id=email.in_reply_to or email.message_id,
                message_id=email.message_id,
                kind="email",
                received_at=email.received_at,
                subject=email.subject,
                in_reply_to=email.in_reply_to,
                references=email.references,
                to_addresses=email.to_addresses,
                raw_event={"reason": match.reason},
            )
            result = await self.feedback.handle(inbound)
            await self._attach_if_missing(email, match, kind="resume_package")
            return InboundOutcome(
                kind="supervisor_feedback",
                handled=result.handled,
                detail={"action": result.action, **result.detail},
            )

        if _is_resume_email(email):
            outcomes = await self.ingestion.ingest_email(email, cycle_id=cycle_id)
            created = [o for o in outcomes if o.created]
            duplicates = [o for o in outcomes if o.is_duplicate]
            return InboundOutcome(
                kind="resume",
                handled=bool(created or duplicates),
                detail={
                    "created": len(created),
                    "duplicates": len(duplicates),
                    "candidates": [o.candidate.name for o in created if o.candidate],
                    "notes": [o.note for o in outcomes if o.note],
                },
            )

        await self.messages.add(
            Message(
                channel=Channel.EMAIL,
                direction=Direction.INBOUND,
                sender=email.from_address,
                recipient=", ".join(email.to_addresses),
                subject=email.subject,
                body=email.body_text,
                external_id=email.message_id,
                thread_key=email.in_reply_to or email.message_id,
                status="received",
                received_at=email.received_at,
                raw={"reason": match.reason},
            )
        )
        await self.events.record(
            "inbound.unmatched",
            actor=Actor.SYSTEM,
            object_type="message",
            object_id=None,
            detail={"from": email.from_address, "subject": email.subject, "reason": match.reason},
            note="无法归类为简历/候选人回复/主管反馈，已忽略",
        )
        await self.session.commit()
        return InboundOutcome(kind="unmatched", handled=False, detail={"reason": match.reason})

    # -------------------------------------------------------- 候选人回信
    async def _handle_candidate_reply(self, email: InboundEmail, match: ThreadMatch) -> InboundOutcome:
        candidate = match.candidate
        assert candidate is not None
        position = match.position or (
            await self.session.get(Position, candidate.position_id) if candidate.position_id else None
        )
        position_title = position.title if position else ""

        reply = await self.extraction.extract_candidate_reply(
            email.body_text,
            candidate_name=candidate.name,
            position_title=position_title,
            current_interview_time=format_interview_time(candidate.interview_time, self.settings.timezone),
        )
        proposed_utc = to_utc_naive(reply.proposed_time) if reply.proposed_time else None

        await self.messages.add(
            Message(
                channel=Channel.EMAIL,
                direction=Direction.INBOUND,
                sender=email.from_address,
                recipient=", ".join(email.to_addresses),
                subject=email.subject,
                body=email.body_text,
                external_id=email.message_id,
                thread_key=email.in_reply_to or email.message_id,
                candidate_id=candidate.id,
                position_id=candidate.position_id,
                parsed=reply.model_dump(mode="json"),
                confidence=reply.confidence,
                status="received",
                received_at=email.received_at,
                raw={"attachments": [a.filename for a in email.attachments]},
            )
        )
        await self._attach_if_missing(email, match, kind="interview_notice")

        from_state = candidate.status
        action = f"candidate.reply.{reply.intent}"
        if reply.intent == "reschedule":
            candidate.status = CandidateStatus.RESCHEDULE_PENDING
        elif reply.intent == "decline":
            candidate.status = CandidateStatus.DECLINED
        elif reply.intent == "confirm":
            candidate.status = CandidateStatus.COMPLETED
        await self.session.flush()

        await self.events.record(
            action,
            actor=Actor.CANDIDATE,
            object_type="candidate",
            object_id=candidate.id,
            from_state=from_state,
            to_state=candidate.status,
            detail={
                "intent": reply.intent,
                "proposed_time": proposed_utc.isoformat() if proposed_utc else None,
                "reason": reply.reason,
                "message_id": email.message_id,
                "method": reply.method,
            },
            note=(reply.reason or "")[:200],
        )

        await self._notify_supervisor(candidate, position, reply, proposed_utc, email.body_text)
        await self.session.commit()

        return InboundOutcome(
            kind=f"candidate_{reply.intent}",
            handled=True,
            detail={
                "candidate": candidate.name,
                "intent": reply.intent,
                "proposed_time": proposed_utc.isoformat() if proposed_utc else None,
                "confidence": reply.confidence,
            },
        )

    async def _notify_supervisor(
        self,
        candidate: Candidate,
        position: Position | None,
        reply,
        proposed_utc,
        raw_message: str,
    ) -> None:
        if position is None or not position.supervisor_open_id:
            return
        hints = {
            "reschedule": "请直接回复新的时间（例如「改到周四下午 3 点」），或回复「同意」按候选人提议安排。",
            "decline": "该候选人已放弃。如需要从本轮名单补面其他人，请直接回复姓名与时间。",
            "confirm": "该候选人已确认参加，无需额外操作。",
            "other": "这条回复我没能判断出明确意向，请您看一眼是否需要处理。",
        }
        await self.notification.notify_supervisor_about_candidate(
            supervisor_open_id=position.supervisor_open_id,
            supervisor_title=position.supervisor_title or position.supervisor_name,
            candidate=candidate,
            position=position,
            intent=reply.intent,
            proposed_time=proposed_utc,
            candidate_message=raw_message,
            action_hint=hints.get(reply.intent, hints["other"]),
        )

    async def _attach_if_missing(self, email: InboundEmail, match: ThreadMatch, *, kind: str) -> None:
        try:
            await self.threads.attach_thread(
                email=email,
                thread=match.thread,
                candidate_id=match.candidate.id if match.candidate else None,
                position_id=match.position.id if match.position else None,
                kind=kind,
            )
        except Exception as exc:  # noqa: BLE001 - 线程补登记失败不影响主流程
            logger.warning("thread.attach_failed", extra=log_extra(error=str(exc)))


def _is_resume_email(email: InboundEmail) -> bool:
    if any(a.filename.lower().endswith(_RESUME_SUFFIXES) for a in email.attachments):
        return True
    text = email.body_text or ""
    if len(text.strip()) < 120:
        return False
    return sum(1 for hint in _RESUME_HINTS if hint in text) >= 2


__all__ = ["InboundEmailService", "InboundOutcome"]
