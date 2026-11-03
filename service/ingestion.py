"""简历收取与入库：IMAP 轮询 / 手动上传两个入口，写入同一张 candidates 表。

职责边界：
- 拿字节 → 存文件 → 抽文本 → 结构化 → 归位职位 → 去重 → 落库 → 调筛选
- 不发送任何消息（那是 ``service/notification.py`` 的事）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from client.mailbox import InboundEmail, MailboxClient
from client.resume_parser import ParsedResume, ResumeParser
from config.settings import Settings
from core.logging import get_logger, log_extra
from core.timeparse import now_utc_naive
from model.candidate import Candidate, CandidateStatus
from model.event import Actor
from model.message import Channel, Direction, Message
from model.position import Position
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from repository.message import MessageRepository
from repository.position import PositionRepository
from schema.extraction import ExtractedCandidate
from service.extraction import ExtractionService
from service.screening import ScreeningService
from schema.screening import ScreeningResult

logger = get_logger(__name__)

_RESUME_SUFFIXES = (".pdf", ".doc", ".docx", ".txt", ".md")


@dataclass
class IngestOutcome:
    candidate: Candidate | None
    created: bool = False
    is_duplicate: bool = False
    position_code: str = ""
    screening: ScreeningResult | None = None
    note: str = ""


class IngestionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        parser: ResumeParser,
        extraction: ExtractionService,
        llm: Any = None,
        company_name: str = "",
    ) -> None:
        self.session = session
        self.settings = settings
        self.parser = parser
        self.extraction = extraction
        self.llm = llm
        self.company_name = company_name
        self.candidates = CandidateRepository(session)
        self.positions = PositionRepository(session)
        self.events = EventRepository(session)
        self.messages = MessageRepository(session)

    # ------------------------------------------------------------------ 入口
    async def ingest_from_mailbox(self, mailbox: MailboxClient, *, cycle_id: int | None = None) -> dict[str, Any]:
        """拉一轮 HR 邮箱，把带简历的邮件全部入库。"""
        emails = await mailbox.poll_inbox()
        outcomes: list[IngestOutcome] = []
        errors: list[str] = []
        for email in emails:
            try:
                outcomes.extend(await self.ingest_email(email, cycle_id=cycle_id))
            except Exception as exc:  # noqa: BLE001 - 单封邮件失败不影响整轮
                logger.warning("ingest.email_failed", extra=log_extra(subject=email.subject, error=str(exc)))
                errors.append(f"{email.subject}: {exc}")
        await self.session.commit()
        return {
            "emails": len(emails),
            "candidates": len([o for o in outcomes if o.candidate]),
            "created": len([o for o in outcomes if o.created]),
            "duplicates": len([o for o in outcomes if o.is_duplicate]),
            "errors": errors,
        }

    async def ingest_email(self, email: InboundEmail, *, cycle_id: int | None = None) -> list[IngestOutcome]:
        """一封邮件可能带多份简历附件；没有附件但正文像简历时也收下。"""
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
                raw={"references": email.references, "attachments": [a.filename for a in email.attachments]},
            )
        )

        resume_attachments = [a for a in email.attachments if a.filename.lower().endswith(_RESUME_SUFFIXES)]
        outcomes: list[IngestOutcome] = []
        for attachment in resume_attachments:
            outcome = await self.ingest_resume_bytes(
                attachment.data,
                attachment.filename,
                source="imap",
                cycle_id=cycle_id,
                contact_hint={"email": email.from_address, "name": email.from_name},
            )
            outcomes.append(outcome)

        if not resume_attachments and _looks_like_resume(email.body_text):
            data = email.body_text.encode("utf-8")
            outcome = await self.ingest_resume_bytes(
                data,
                f"{email.message_id.strip('<>')[:40] or 'email'}.txt",
                source="imap-body",
                cycle_id=cycle_id,
                contact_hint={"email": email.from_address, "name": email.from_name},
            )
            outcomes.append(outcome)

        return outcomes

    async def ingest_resume_bytes(
        self,
        data: bytes,
        filename: str,
        *,
        position_code: str | None = None,
        source: str = "manual",
        cycle_id: int | None = None,
        contact_hint: dict[str, str] | None = None,
        prefetched: ParsedResume | None = None,
    ) -> IngestOutcome:
        """核心入库流程（两个入口共用）。"""
        parsed = prefetched or await self.parser.save_and_parse(data, filename)

        active_positions = await self.positions.list_active()
        if not active_positions:
            return IngestOutcome(candidate=None, note="没有在招职位，简历已保存但无法归位")

        extracted = await self.extraction.extract_candidate(
            parsed.text,
            filename=parsed.filename,
            positions=[(p.code, p.title) for p in active_positions],
        )
        self._apply_contact_hint(extracted, contact_hint)

        position, match_note = self._resolve_position(
            parsed.text, extracted, active_positions, explicit_code=position_code
        )
        if position is None:
            return IngestOutcome(candidate=None, note=f"无法归位到任何在招职位（{match_note}）")

        now = now_utc_naive()
        duplicate = await self.candidates.find_duplicate(email=extracted.email, phone=extracted.phone)
        if duplicate is not None:
            duplicate.last_seen_at = now
            await self.session.flush()
            await self.events.record(
                "candidate.duplicate",
                actor=Actor.ROBOT,
                object_type="candidate",
                object_id=duplicate.id,
                from_state=duplicate.status,
                to_state=duplicate.status,
                detail={
                    "email": extracted.email,
                    "phone": extracted.phone,
                    "resume_filename": parsed.filename,
                    "position_code": position.code,
                },
                note="重复投递：保留首次记录，本次不覆盖",
            )
            await self.session.commit()
            return IngestOutcome(
                candidate=duplicate,
                is_duplicate=True,
                position_code=duplicate.position_code,
                note="重复投递，已追加事件",
            )

        candidate = Candidate(
            name=extracted.name or _fallback_name(parsed.filename, extracted.email),
            email=extracted.email,
            phone=extracted.phone,
            position_id=position.id,
            position_code=position.code,
            department=position.department,
            education_level=extracted.education_level,
            school=extracted.school,
            major=extracted.major,
            years_experience=extracted.years_experience,
            skills=list(extracted.skills or []),
            project_highlights=list(extracted.project_highlights or []),
            summary=extracted.summary,
            structured=extracted.model_dump(mode="json"),
            resume_path=parsed.path,
            resume_filename=parsed.filename,
            resume_text=parsed.text[:50_000],
            source=source,
            status=CandidateStatus.RECEIVED,
            first_seen_at=now,
            last_seen_at=now,
            cycle_id=cycle_id,
        )
        await self.candidates.add(candidate)

        await self.events.record(
            "candidate.received",
            actor=Actor.ROBOT,
            object_type="candidate",
            object_id=candidate.id,
            to_state=CandidateStatus.RECEIVED,
            detail={
                "source": source,
                "resume_filename": parsed.filename,
                "extract_engine": parsed.engine,
                "parser_warnings": parsed.warnings,
                "position_match": match_note,
            },
        )

        from_state = candidate.status
        candidate.status = CandidateStatus.PARSED
        await self.session.flush()
        await self.events.record(
            "candidate.parsed",
            actor=Actor.ROBOT,
            object_type="candidate",
            object_id=candidate.id,
            from_state=from_state,
            to_state=CandidateStatus.PARSED,
            detail={
                "education_level": candidate.education_level,
                "school": candidate.school,
                "major": candidate.major,
                "years_experience": candidate.years_experience,
                "skills": candidate.skills,
                "confidence": extracted.confidence,
            },
        )

        screening_service = ScreeningService(self.session, llm=self.llm, company_name=self.company_name)
        result = await screening_service.screen_candidate(candidate, position)
        await self.session.commit()

        logger.info(
            "ingest.candidate",
            extra=log_extra(
                candidate_id=candidate.id,
                position=position.code,
                score=result.score,
                passed=result.passed,
            ),
        )
        return IngestOutcome(
            candidate=candidate,
            created=True,
            position_code=position.code,
            screening=result,
            note=match_note,
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _apply_contact_hint(extracted: ExtractedCandidate, hint: dict[str, str] | None) -> None:
        if not hint:
            return
        if not extracted.email and hint.get("email"):
            extracted.email = hint["email"]
        if not extracted.name and hint.get("name"):
            extracted.name = hint["name"]

    def _resolve_position(
        self,
        resume_text: str,
        extracted: ExtractedCandidate,
        positions: Sequence[Position],
        *,
        explicit_code: str | None = None,
    ) -> tuple[Position | None, str]:
        by_code = {p.code: p for p in positions}

        if explicit_code:
            if explicit_code in by_code:
                return by_code[explicit_code], f"指定职位 {explicit_code}"
            # 允许传职位名称
            for position in positions:
                if position.title == explicit_code:
                    return position, f"指定职位名称 {explicit_code}"

        hint = (extracted.position_hint or "").strip()
        if hint:
            for position in positions:
                if hint == position.title or hint in position.title or position.title in hint:
                    return position, f"简历注明岗位「{hint}」"

        haystack = f"{resume_text[:4000]} {hint}".lower()
        best: tuple[int, Position] | None = None
        for position in positions:
            keywords = [kw for kw in (position.keywords or []) if kw]
            hits = sum(1 for kw in keywords if kw.lower() in haystack)
            if hits and (best is None or hits > best[0]):
                best = (hits, position)

        if best is not None:
            hits, position = best
            return position, f"关键词命中 {hits} 个 → {position.code}"

        if len(positions) == 1:
            return positions[0], "仅有一个在招职位，默认归位"
        return None, "简历未写明岗位且关键词不足"


def _looks_like_resume(text: str) -> bool:
    if not text or len(text.strip()) < 120:
        return False
    markers = ("简历", "应聘", "求职", "教育背景", "工作经历", "项目经验", "期望职位")
    return sum(1 for marker in markers if marker in text) >= 2


def _fallback_name(filename: str, email: str) -> str:
    stem = Path(filename).stem
    if stem and len(stem) <= 20:
        return stem
    return email.split("@")[0] if email else "未命名候选人"


def build_search_summary(candidates: Sequence[Candidate]) -> dict[str, int]:
    """按状态汇总（给 /recruitment/run 的响应和日志用）。"""
    summary: dict[str, int] = {}
    for candidate in candidates:
        summary[candidate.status] = summary.get(candidate.status, 0) + 1
    return summary


__all__ = ["IngestOutcome", "IngestionService", "build_search_summary"]
