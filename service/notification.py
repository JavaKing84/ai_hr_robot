"""出站编排：给主管发简历包（飞书 + 邮件）、给候选人发面试通知、发回执与改期确认。

产品文档规定「邮件发出即本轮任务完成」——所以本模块是闭环的终点，
不做任何越界动作（不发感谢信、不做测评通知、不建人才库）。
"""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from client.feishu import FeishuClient
from client.mailbox import MailAttachment, MailboxClient
from config.settings import Settings
from core.logging import get_logger, log_extra
from core.timeparse import format_interview_time, now_utc_naive
from model.candidate import Candidate, CandidateStatus
from model.email_thread import EmailThread, ThreadKind
from model.event import Actor
from model.message import Channel, Direction, Message
from model.position import Position
from repository.candidate import CandidateRepository
from repository.email_thread import EmailThreadRepository
from repository.event import EventRepository
from repository.message import MessageRepository
from schema.candidate import PackageDispatchResult
from schema.config import CompanyInfo, MailboxConfig

logger = get_logger(__name__)


@dataclass
class NoticeResult:
    ok: bool
    message_id: str = ""
    detail: str = ""
    # True = 只生成了草稿并挂起，实际没有投递（等人工确认）
    pending: bool = False


class NotificationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        feishu: FeishuClient,
        mailbox: MailboxClient,
        company: CompanyInfo | None = None,
        mailbox_config: MailboxConfig | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.feishu = feishu
        self.mailbox = mailbox
        self.company = company or CompanyInfo()
        self.mailbox_config = mailbox_config or MailboxConfig()
        self.candidates = CandidateRepository(session)
        self.positions = None  # 由调用方按需注入（避免循环依赖）
        self.threads = EmailThreadRepository(session)
        self.events = EventRepository(session)
        self.messages = MessageRepository(session)
        self._env = Environment(
            loader=FileSystemLoader(str(settings.templates_dir)),
            autoescape=select_autoescape(enabled_extensions=("html",), default=False),
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )

    # ------------------------------------------------------------------ 渲染
    def render(self, template_name: str, **context: object) -> str:
        template = self._env.get_template(template_name)
        return template.render(**context).strip() + "\n"

    # ------------------------------------------------- 分发：简历包 → 主管
    async def dispatch_cycle(self, cycle_id: int) -> list[PackageDispatchResult]:
        """把本轮「筛选通过」的候选人按职位打包，发给对应主管（飞书 + 邮件副本）。"""
        results: list[PackageDispatchResult] = []
        candidates = await self.candidates.list_by_cycle(cycle_id)
        passed = [c for c in candidates if c.status == CandidateStatus.SCREENED_PASSED]
        by_position: dict[int, list[Candidate]] = {}
        for candidate in passed:
            if candidate.position_id:
                by_position.setdefault(candidate.position_id, []).append(candidate)

        for position_id, group in by_position.items():
            position = await self.session.get(Position, position_id)
            if position is None:
                continue
            received_count = len([c for c in candidates if c.position_id == position_id])
            try:
                result = await self._dispatch_position(cycle_id, position, group, received_count)
            except Exception as exc:  # noqa: BLE001 - 单职位失败不影响其他部门
                logger.exception("dispatch.failed", extra=log_extra(cycle_id=cycle_id, position=position.code))
                result = PackageDispatchResult(
                    position_code=position.code,
                    position_title=position.title,
                    supervisor=position.supervisor_name,
                    received_count=received_count,
                    passed_count=len(group),
                    ok=False,
                    error=str(exc),
                )
            results.append(result)
        return results

    async def _dispatch_position(
        self,
        cycle_id: int,
        position: Position,
        candidates: Sequence[Candidate],
        received_count: int,
    ) -> PackageDispatchResult:
        package_path, package_name = self._build_package(cycle_id, position, candidates, received_count)

        roster = [
            {
                "name": c.name,
                "education_level": c.education_level or "—",
                "school": c.school or "—",
                "major": c.major or "—",
                "years_experience": _fmt_years(c.years_experience),
                "score": round(c.score, 1),
            }
            for c in candidates
        ]
        summary_text = self.render(
            "feishu_dispatch.md",
            supervisor_title=position.supervisor_title or position.supervisor_name,
            position_title=position.title,
            received_count=received_count,
            passed_count=len(candidates),
            package_name=package_name,
            candidates=roster,
            company_name=self.company.name,
        )

        feishu_text = await self.feishu.push_text(position.supervisor_open_id, summary_text)
        await self._record_feishu_out(
            recipient=self._feishu_recipient(position.supervisor_open_id),
            body=summary_text,
            message_id=feishu_text.message_id,
            ok=feishu_text.ok,
            error=feishu_text.error,
            position_id=position.id,
            parsed={"kind": "resume_package_notice", "passed_count": len(candidates)},
        )

        file_result = await self.feishu.push_file(
            position.supervisor_open_id, package_path, file_name=package_name
        )
        await self._record_feishu_out(
            recipient=self._feishu_recipient(position.supervisor_open_id),
            body=f"[简历压缩包] {package_name}",
            message_id=file_result.message_id,
            ok=file_result.ok,
            error=file_result.error,
            position_id=position.id,
            parsed={"kind": "resume_package_file", "path": str(package_path)},
        )

        email_result = await self._send_supervisor_email(position, candidates, package_path, package_name, received_count)

        for candidate in candidates:
            from_state = candidate.status
            candidate.status = CandidateStatus.DISPATCHED
            candidate.cycle_id = cycle_id
            await self.events.record(
                "candidate.dispatched",
                actor=Actor.ROBOT,
                object_type="candidate",
                object_id=candidate.id,
                from_state=from_state,
                to_state=CandidateStatus.DISPATCHED,
                detail={
                    "position_code": position.code,
                    "supervisor": position.supervisor_name,
                    "feishu_message_id": feishu_text.message_id,
                    "email_message_id": email_result.message_id,
                },
            )
        await self.session.commit()

        return PackageDispatchResult(
            position_code=position.code,
            position_title=position.title,
            supervisor=position.supervisor_name,
            received_count=received_count,
            passed_count=len(candidates),
            candidate_names=[c.name for c in candidates],
            package_path=str(package_path),
            feishu_message_id=feishu_text.message_id,
            email_message_id=email_result.message_id,
            ok=feishu_text.ok and file_result.ok,
            error="; ".join(filter(None, [feishu_text.error, file_result.error, email_result.detail])),
        )

    def _build_package(
        self,
        cycle_id: int,
        position: Position,
        candidates: Sequence[Candidate],
        received_count: int,
    ) -> tuple[Path, str]:
        """把该职位的简历打成 zip（含一份 summary.md），文件名体现部门与职位。"""
        package_dir = self.settings.packages_dir / str(cycle_id)
        package_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        package_name = f"{position.department}-{position.title}-简历{len(candidates)}份-{timestamp}.zip"
        package_path = package_dir / package_name

        summary = self.render(
            "resume_package_summary.md",
            company_name=self.company.name,
            department=position.department,
            position_title=position.title,
            supervisor_name=position.supervisor_name,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            received_count=received_count,
            passed_count=len(candidates),
            rule_version=position.rule_version,
            threshold=int(position.threshold),
            candidates=[
                {
                    "name": c.name,
                    "score": round(c.score, 1),
                    "education_level": c.education_level,
                    "school": c.school,
                    "major": c.major,
                    "years_experience": _fmt_years(c.years_experience),
                    "email": c.email,
                    "phone": c.phone,
                    "skills": "、".join(c.skills or []) or "—",
                    "project_highlights": "；".join(c.project_highlights or []) or "—",
                    "hit_detail": (c.score_detail or {}).get("reason", ""),
                    "resume_filename": c.resume_filename,
                    **_deep_fields(c),
                }
                for c in candidates
            ],
        )

        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("00-筛选说明.md", summary)
            for index, candidate in enumerate(candidates, start=1):
                resume_path = Path(candidate.resume_path) if candidate.resume_path else None
                if resume_path and resume_path.exists():
                    arcname = f"{index:02d}-{candidate.name or '候选人'}-{resume_path.name}"
                    archive.write(resume_path, arcname=arcname)
        logger.info(
            "package.built",
            extra=log_extra(cycle_id=cycle_id, position=position.code, path=str(package_path)),
        )
        return package_path, package_name

    async def _send_supervisor_email(
        self,
        position: Position,
        candidates: Sequence[Candidate],
        package_path: Path,
        package_name: str,
        received_count: int,
    ) -> NoticeResult:
        """把简历包另发一份到主管邮箱：这样主管「直接在原邮件上回复」这条路径才成立。"""
        if not position.supervisor_email:
            return NoticeResult(ok=False, detail="主管未配置邮箱，跳过邮件副本")

        subject = (
            f"【简历筛选】{position.department}·{position.title} 本轮通过 {len(candidates)} 份"
            f"（共收到 {received_count} 份）"
        )
        body = (
            f"{position.supervisor_title or position.supervisor_name}，您好：\n\n"
            f"本轮 {position.department}「{position.title}」共收到 {received_count} 份简历，"
            f"经过智能筛选符合条件的为 {len(candidates)} 份，见附件压缩包。\n\n"
            f"通过筛选的候选人：{'、'.join(c.name for c in candidates)}\n\n"
            "请直接回复本邮件告知面试名单与时间，例如：\n"
            "「请通知张三、李四，下周三下午 14:00 来公司面试」\n\n"
            f"{self.company.name} HR 机器人\n"
        )

        token = uuid.uuid4().hex[:16]
        reply_to = self.mailbox.reply_address(token)
        message_id = self.mailbox.new_message_id()
        attachment = MailAttachment(
            filename=package_name,
            content_type="application/zip",
            data=package_path.read_bytes(),
        )
        result = await self.mailbox.send(
            to=position.supervisor_email,
            subject=subject,
            body_text=body,
            body_html=markdown_to_html(body),
            message_id=message_id,
            reply_to=reply_to,
            attachments=[attachment],
        )

        thread = await self.threads.create(
            token=token,
            root_message_id=message_id,
            kind=ThreadKind.RESUME_PACKAGE,
            subject=subject,
            reply_address=reply_to,
            counterpart_email=position.supervisor_email,
            position_id=position.id,
            meta={"package_name": package_name, "passed_count": len(candidates)},
        )
        await self._record_email_out(
            recipient=position.supervisor_email,
            subject=subject,
            body=body,
            message_id=message_id,
            ok=result.ok,
            error=result.error,
            position_id=position.id,
            thread_key=thread.token,
        )
        if not result.ok:
            logger.warning("mail.supervisor_copy_failed", extra=log_extra(error=result.error))
        return NoticeResult(ok=result.ok, message_id=message_id, detail=result.error)

    # --------------------------------------------- 面试通知：候选人（邮件）
    async def send_interview_notice(
        self,
        candidate: Candidate,
        position: Position,
        *,
        interview_time: datetime,
        location: str | None = None,
        notes: str = "",
        message_id: str | None = None,
        confirmed: bool = False,
    ) -> NoticeResult:
        """发送单封面试通知。

        ``confirmed=False``（默认）时受 ``interview_notice_require_confirm`` 管控：
        只生成草稿并挂起为 ``pending_approval``，绝不投递；只有 HR 走
        ``confirm_pending_interview_notices()`` 显式确认后才会真正发出。
        """
        if not candidate.email:
            return NoticeResult(ok=False, detail="候选人缺邮箱，无法发送面试通知")

        when = format_interview_time(interview_time, self.settings.timezone)
        place = location or self.mailbox_config.interview_location or "地点待定"
        body = self.render(
            "interview_notice.md",
            candidate_name=candidate.name or "候选人",
            company_name=self.company.name,
            position_title=position.title,
            interview_time=when,
            location=place,
            hr_contact=self.company.hr_contact or f"{self.company.hr_name}（{self.company.hr_title}）",
            notes=notes,
            hr_signature=self._signature(),
        )
        subject = f"面试通知 | {self.company.name} | {position.title} | {when}"

        thread = await self.threads.find_for_candidate(candidate.id, kind=ThreadKind.INTERVIEW_NOTICE)
        token = thread.token if thread else uuid.uuid4().hex[:16]
        # 幂等键（§4-A.1a）：默认仍由 mailbox.new_message_id() 生成；补发时复用原 Message-ID
        message_id = message_id or self.mailbox.new_message_id()
        reply_to = self.mailbox.reply_address(token)

        # 幂等防重：同一 Message-ID 已成功发出（sent/mocked）→ 跳过，绝不发第二封
        delivered = await self.messages.find_delivered(message_id)
        if delivered is not None:
            logger.info(
                "interview.notice.duplicate_skipped",
                extra=log_extra(candidate_id=candidate.id, message_id=message_id),
            )
            return NoticeResult(ok=True, message_id=message_id, detail="已发送过，幂等跳过")

        # 人工确认闸门（测试期默认开启）：只挂起草稿，不调用 SMTP
        if self.settings.interview_notice_require_confirm and not confirmed:
            return await self._park_interview_notice(
                candidate,
                position,
                subject=subject,
                body=body,
                interview_time=interview_time,
                location=place,
                message_id=message_id,
                token=token,
            )

        result = await self.mailbox.send(
            to=candidate.email,
            subject=subject,
            body_text=body,
            body_html=markdown_to_html(body),
            message_id=message_id,
            reply_to=reply_to,
        )
        if thread is None:
            thread = await self.threads.create(
                token=token,
                root_message_id=message_id,
                kind=ThreadKind.INTERVIEW_NOTICE,
                subject=subject,
                reply_address=reply_to,
                counterpart_email=candidate.email,
                candidate_id=candidate.id,
                position_id=position.id,
                meta={"interview_time": interview_time.isoformat()},
            )
        elif message_id not in (thread.message_ids or []):
            await self.threads.append_message_id(thread, message_id)

        from_state = candidate.status
        candidate.status = CandidateStatus.INTERVIEW_NOTIFIED
        candidate.interview_time = interview_time
        candidate.interview_location = place
        await self.session.flush()

        await self._record_email_out(
            recipient=candidate.email,
            subject=subject,
            body=body,
            message_id=message_id,
            ok=result.ok,
            error=result.error,
            candidate_id=candidate.id,
            position_id=position.id,
            thread_key=thread.token,
        )
        await self.events.record(
            "interview.notice_sent" if result.ok else "interview.notice_failed",
            actor=Actor.ROBOT,
            object_type="candidate",
            object_id=candidate.id,
            from_state=from_state,
            to_state=candidate.status,
            detail={
                "position_code": position.code,
                "interview_time": interview_time.isoformat(),
                "location": place,
                "message_id": message_id,
                "mode": result.mode if hasattr(result, "mode") else self.settings.mail_mode,
                "error": result.error,
            },
        )
        if not result.ok:
            logger.warning(
                "interview.notice.send_failed",
                extra=log_extra(candidate_id=candidate.id, message_id=message_id, error=result.error),
            )
        await self.session.commit()
        return NoticeResult(ok=result.ok, message_id=message_id, detail=result.error)

    # ------------------------------------------------- 人工确认闸门（测试期）
    async def _park_interview_notice(
        self,
        candidate: Candidate,
        position: Position,
        *,
        subject: str,
        body: str,
        interview_time: datetime,
        location: str,
        message_id: str,
        token: str,
    ) -> NoticeResult:
        """把面试通知存成 pending_approval 草稿，等 HR 确认后再发。

        不动 smtp、不写「已发送」，候选人状态停在 INTERVIEW_PENDING_CONFIRM。
        确认入口见 ``confirm_pending_interview_notices()``。
        """
        from_state = candidate.status
        candidate.status = CandidateStatus.INTERVIEW_PENDING_CONFIRM
        candidate.interview_time = interview_time
        candidate.interview_location = location
        await self.session.flush()

        await self._record_email_out(
            recipient=candidate.email,
            subject=subject,
            body=body,
            message_id=message_id,
            ok=True,
            candidate_id=candidate.id,
            position_id=position.id,
            thread_key=token,
            status="pending_approval",
        )
        await self.events.record(
            "interview.notice_pending_approval",
            actor=Actor.ROBOT,
            object_type="candidate",
            object_id=candidate.id,
            from_state=from_state,
            to_state=candidate.status,
            detail={
                "position_code": position.code,
                "interview_time": interview_time.isoformat(),
                "location": location,
                "message_id": message_id,
                "recipient": candidate.email,
                "subject": subject,
            },
            note="面试通知已生成草稿，等待 HR 人工确认后发送（未投递）",
        )
        await self.session.commit()
        logger.info(
            "interview.notice.parked",
            extra=log_extra(candidate_id=candidate.id, recipient=candidate.email),
        )
        return NoticeResult(
            ok=True,
            message_id=message_id,
            detail="已挂起：等待人工确认后发送（未投递）",
            pending=True,
        )

    async def pending_interview_notices(self) -> list[Message]:
        """列出所有待确认的面试通知草稿（按创建顺序）。"""
        result = await self.session.execute(
            select(Message)
            .where(
                Message.channel == Channel.EMAIL,
                Message.direction == Direction.OUTBOUND,
                Message.status == "pending_approval",
            )
            .order_by(Message.id)
        )
        return list(result.scalars().all())

    async def confirm_pending_interview_notices(
        self, message_ids: Sequence[int] | None = None
    ) -> list[NoticeResult]:
        """HR 确认后，把挂起的面试通知真正发出去。

        ``message_ids`` 为空表示发送全部待确认项；发送成功后原草稿标记 superseded，
        并沿用原 Message-ID，天然幂等（重复确认不会发第二封）。
        """
        pending = await self.pending_interview_notices()
        if message_ids:
            wanted = {int(mid) for mid in message_ids}
            pending = [row for row in pending if row.id in wanted]

        results: list[NoticeResult] = []
        for record in pending:
            candidate = (
                await self.session.get(Candidate, record.candidate_id)
                if record.candidate_id
                else None
            )
            position = (
                await self.session.get(Position, record.position_id) if record.position_id else None
            )
            if candidate is None or position is None or not candidate.interview_time:
                logger.warning(
                    "interview.notice.confirm_skipped",
                    extra=log_extra(message_id=record.external_id),
                )
                continue

            result = await self.send_interview_notice(
                candidate,
                position,
                interview_time=candidate.interview_time,
                location=candidate.interview_location,
                message_id=record.external_id,
                confirmed=True,
            )
            record.status = "superseded"
            await self.session.flush()
            results.append(result)

        await self.session.commit()
        return results

    async def request_interview_approval(
        self,
        open_id: str,
        *,
        supervisor_title: str = "HR",
    ) -> NoticeResult:
        """在飞书向 HR 本人发一条「是否确认发送」的请示（不投递任何邮件）。

        只在存在 ``pending_approval`` 草稿时才有意义；HR 回复「确认 / 确认 1,3 / 取消」
        由 ``FeedbackService._handle_interview_approval()`` 接住并调用
        ``confirm_pending_interview_notices()`` / ``cancel_pending_interview_notices()``。
        """
        pending = await self.pending_interview_notices()
        if not pending:
            return NoticeResult(ok=False, detail="当前没有待确认的面试通知")

        items: list[dict[str, object]] = []
        first_time: datetime | None = None
        first_location = ""
        for index, record in enumerate(pending, start=1):
            candidate = (
                await self.session.get(Candidate, record.candidate_id) if record.candidate_id else None
            )
            position = (
                await self.session.get(Position, record.position_id) if record.position_id else None
            )
            if candidate is not None and first_time is None:
                first_time = candidate.interview_time
                first_location = candidate.interview_location or ""
            score = candidate.score if candidate is not None else None
            items.append(
                {
                    "index": index,
                    "name": (candidate.name if candidate else "") or "未命名",
                    "email": (candidate.email if candidate else "") or record.recipient or "（缺邮箱）",
                    "position_title": position.title if position else "（未归位）",
                    "score": f"{score:.1f}" if score is not None else "-",
                }
            )

        text = self.render(
            "feishu_approval_request.md",
            supervisor_title=supervisor_title or "HR",
            count=len(items),
            items=items,
            interview_time=(
                format_interview_time(first_time, self.settings.timezone) if first_time else "待定"
            ),
            location=first_location or self.mailbox_config.interview_location or "地点待定",
            company_name=self.company.name,
        )
        result = await self.feishu.push_text(open_id, text)
        await self._record_feishu_out(
            recipient=self._feishu_recipient(open_id),
            body=text,
            message_id=result.message_id,
            ok=result.ok,
            error=result.error,
            parsed={
                "kind": "interview_approval_request",
                "pending_ids": [row.id for row in pending],
            },
        )
        await self.events.record(
            "interview.approval_requested",
            actor=Actor.ROBOT,
            object_type="message",
            object_id=None,
            detail={
                "open_id": open_id,
                "pending_count": len(items),
                "pending_ids": [row.id for row in pending],
                "feishu_ok": result.ok,
                "feishu_error": result.error,
            },
            note=f"已通过飞书向 HR 请示：{len(items)} 份面试通知待确认，暂未投递",
        )
        await self.session.commit()
        logger.info(
            "interview.approval.requested",
            extra=log_extra(open_id=open_id, pending=len(items), ok=result.ok),
        )
        return NoticeResult(ok=result.ok, message_id=result.message_id, detail=result.error)

    async def cancel_pending_interview_notices(
        self, message_ids: Sequence[int] | None = None
    ) -> int:
        """HR 在飞书回复「取消」：把挂起的草稿作废，候选人退回已分发状态。"""
        pending = await self.pending_interview_notices()
        if message_ids:
            wanted = {int(mid) for mid in message_ids}
            pending = [row for row in pending if row.id in wanted]

        cancelled = 0
        for record in pending:
            candidate = (
                await self.session.get(Candidate, record.candidate_id)
                if record.candidate_id
                else None
            )
            from_state = candidate.status if candidate is not None else None
            if candidate is not None and candidate.status == CandidateStatus.INTERVIEW_PENDING_CONFIRM:
                candidate.status = CandidateStatus.DISPATCHED
                candidate.interview_time = None
                candidate.interview_location = ""
            record.status = "cancelled"
            await self.events.record(
                "interview.notice_cancelled",
                actor=Actor.HR,
                object_type="candidate",
                object_id=record.candidate_id,
                from_state=from_state,
                to_state=candidate.status if candidate is not None else None,
                detail={
                    "message_id": record.external_id,
                    "recipient": record.recipient,
                    "subject": record.subject,
                },
                note="HR 在飞书回复取消，面试通知作废且未投递",
            )
            cancelled += 1

        await self.session.commit()
        return cancelled

    async def send_interview_notices(
        self,
        candidates: Iterable[Candidate],
        position: Position,
        *,
        interview_time: datetime,
        location: str | None = None,
        notes: str = "",
    ) -> list[NoticeResult]:
        results = []
        for candidate in candidates:
            results.append(
                await self.send_interview_notice(
                    candidate, position, interview_time=interview_time, location=location, notes=notes
                )
            )
        return results

    # ------------------------------------------------- 幂等补发（§4-A.1(b)）
    async def retry_failed_interview_notices(self) -> list[NoticeResult]:
        """补发历史上发送失败的面试通知邮件。

        范式：失败时不立即重试（候选人绝不连收多封），由下一轮 / 人工触发本入口；
        每条失败记录按**原 Message-ID** 走 send_interview_notice 的幂等检查后补发，
        已成功发出的自动跳过，因此补发不产生重复邮件。
        """
        failed = await self.messages.list_failed_outbound(channel=Channel.EMAIL)
        results: list[NoticeResult] = []
        for record in failed:
            # 主管简历包邮件（candidate_id 为空）的失败由 dispatch_cycle 处理，不在本入口补发
            if not record.candidate_id:
                continue
            candidate = await self.candidates.get(record.candidate_id)
            if candidate is None or not candidate.email or not candidate.interview_time or not candidate.position_id:
                logger.warning(
                    "retry.notice.skipped",
                    extra=log_extra(message_id=record.external_id, reason="候选人/时间/职位信息缺失"),
                )
                continue
            # 改期确认邮件依赖主管已确认的新时间（I-06），自动补发有越权风险，留给人工处理
            if "调整" in record.subject:
                logger.warning(
                    "retry.reschedule_manual",
                    extra=log_extra(message_id=record.external_id, candidate_id=candidate.id),
                )
                continue
            position = await self.session.get(Position, candidate.position_id)
            if position is None:
                continue
            result = await self.send_interview_notice(
                candidate, position, interview_time=candidate.interview_time, message_id=record.external_id
            )
            logger.info(
                "retry.notice.done",
                extra=log_extra(
                    candidate_id=candidate.id,
                    message_id=record.external_id,
                    ok=result.ok,
                    detail=result.detail,
                ),
            )
            results.append(result)
        return results

    async def retry_failed_feishu_messages(self) -> int:
        """补发失败的出站飞书消息，返回实际补发条数。

        飞书 message_id 由服务端在发送成功后才生成，无法预生成幂等键，
        因此按「收件人 + 正文精确匹配」查重（应用层查表，§4-A.1a 边界内）：
        同内容已成功发出的不再重发；简历包文件消息无法用文本通道补发，留给人工。
        """
        failed = await self.messages.list_failed_outbound(channel=Channel.FEISHU)
        resent = 0
        for record in failed:
            if not record.recipient or not record.body:
                continue
            if (record.parsed or {}).get("kind") == "resume_package_file":
                logger.warning(
                    "retry.feishu_file_manual",
                    extra=log_extra(recipient=record.recipient, message_id=record.external_id),
                )
                continue
            delivered = await self.messages.find_delivered_by_content(
                channel=Channel.FEISHU, recipient=record.recipient, body=record.body
            )
            if delivered is not None:
                continue
            result = await self.feishu.push_text(record.recipient, record.body)
            await self._record_feishu_out(
                recipient=record.recipient,
                body=record.body,
                message_id=result.message_id,
                ok=result.ok,
                error=result.error,
                position_id=record.position_id,
                candidate_id=record.candidate_id,
                thread_key=record.thread_key,
                parsed=record.parsed or {"kind": "retry"},
            )
            if result.ok:
                resent += 1
                logger.info(
                    "retry.feishu.done",
                    extra=log_extra(recipient=record.recipient, message_id=result.message_id),
                )
            else:
                logger.warning(
                    "retry.feishu.failed",
                    extra=log_extra(recipient=record.recipient, error=result.error),
                )
        await self.session.commit()
        return resent

    async def send_reschedule_confirmation(
        self,
        candidate: Candidate,
        position: Position,
        *,
        new_time: datetime,
        old_time: datetime | None,
        location: str | None = None,
    ) -> NoticeResult:
        if not candidate.email:
            return NoticeResult(ok=False, detail="候选人缺邮箱")

        when = format_interview_time(new_time, self.settings.timezone)
        place = location or candidate.interview_location or self.mailbox_config.interview_location or "地点待定"
        body = self.render(
            "interview_update.md",
            candidate_name=candidate.name or "候选人",
            old_interview_time=format_interview_time(old_time, self.settings.timezone) if old_time else "（未记录）",
            interview_time=when,
            location=place,
            position_title=position.title,
            hr_contact=self.company.hr_contact or f"{self.company.hr_name}（{self.company.hr_title}）",
            hr_signature=self._signature(),
        )
        subject = f"面试时间调整确认 | {self.company.name} | {position.title} | {when}"

        thread = await self.threads.find_for_candidate(candidate.id, kind=ThreadKind.INTERVIEW_NOTICE)
        token = thread.token if thread else uuid.uuid4().hex[:16]
        message_id = self.mailbox.new_message_id()
        reply_to = self.mailbox.reply_address(token)
        references = list(thread.message_ids or []) if thread else []

        result = await self.mailbox.send(
            to=candidate.email,
            subject=subject,
            body_text=body,
            body_html=markdown_to_html(body),
            message_id=message_id,
            in_reply_to=references[-1] if references else "",
            references=references,
            reply_to=reply_to,
        )
        if thread is None:
            thread = await self.threads.create(
                token=token,
                root_message_id=message_id,
                kind=ThreadKind.INTERVIEW_NOTICE,
                subject=subject,
                reply_address=reply_to,
                counterpart_email=candidate.email,
                candidate_id=candidate.id,
                position_id=position.id,
            )
        else:
            await self.threads.append_message_id(thread, message_id)

        from_state = candidate.status
        candidate.status = CandidateStatus.INTERVIEW_NOTIFIED
        candidate.interview_time = new_time
        candidate.interview_location = place
        await self.session.flush()

        await self._record_email_out(
            recipient=candidate.email,
            subject=subject,
            body=body,
            message_id=message_id,
            ok=result.ok,
            error=result.error,
            candidate_id=candidate.id,
            position_id=position.id,
            thread_key=thread.token,
        )
        await self.events.record(
            "interview.rescheduled",
            actor=Actor.ROBOT,
            object_type="candidate",
            object_id=candidate.id,
            from_state=from_state,
            to_state=candidate.status,
            detail={"new_time": new_time.isoformat(), "old_time": old_time.isoformat() if old_time else None},
        )
        await self.session.commit()
        return NoticeResult(ok=result.ok, message_id=message_id, detail=result.error)

    # ------------------------------------------------------ 飞书：给主管的
    async def notify_supervisor(
        self,
        open_id: str,
        text: str,
        *,
        position_id: int | None = None,
        candidate_id: int | None = None,
        thread_key: str = "",
        kind: str = "generic",
    ) -> NoticeResult:
        result = await self.feishu.push_text(open_id, text)
        await self._record_feishu_out(
            recipient=self._feishu_recipient(open_id),
            body=text,
            message_id=result.message_id,
            ok=result.ok,
            error=result.error,
            position_id=position_id,
            candidate_id=candidate_id,
            thread_key=thread_key,
            parsed={"kind": kind},
        )
        await self.session.commit()
        return NoticeResult(ok=result.ok, message_id=result.message_id, detail=result.error)

    def _feishu_recipient(self, open_id: str) -> str:
        """留痕里的收件人：群模式下记 feishu-group，避免把「群」写成某个人的 open_id。"""
        return "feishu-group" if self.settings.feishu_group_push else open_id

    async def send_receipt_to_supervisor(
        self,
        supervisor_open_id: str,
        *,
        supervisor_title: str,
        position: Position,
        candidates: Sequence[Candidate],
        interview_time: datetime,
        location: str,
    ) -> NoticeResult:
        text = self.render(
            "feishu_receipt.md",
            supervisor_title=supervisor_title,
            candidate_count=len(candidates),
            candidates=[{"name": c.name, "email": c.email} for c in candidates],
            interview_time=format_interview_time(interview_time, self.settings.timezone),
            location=location,
            position_title=position.title,
            company_name=self.company.name,
        )
        return await self.notify_supervisor(
            supervisor_open_id,
            text,
            position_id=position.id,
            kind="interview_receipt",
        )

    async def notify_supervisor_about_candidate(
        self,
        *,
        supervisor_open_id: str,
        supervisor_title: str,
        candidate: Candidate,
        position: Position,
        intent: str,
        proposed_time: datetime | None,
        candidate_message: str,
        action_hint: str,
    ) -> NoticeResult:
        intent_labels = {
            "reschedule": "希望改期",
            "decline": "放弃面试",
            "confirm": "确认参加",
            "other": "其他反馈",
        }
        text = self.render(
            "feishu_candidate_reply.md",
            supervisor_title=supervisor_title,
            position_title=position.title,
            candidate_name=candidate.name or "候选人",
            intent_label=intent_labels.get(intent, intent),
            proposed_time=format_interview_time(proposed_time, self.settings.timezone) if proposed_time else "",
            current_interview_time=format_interview_time(candidate.interview_time, self.settings.timezone),
            candidate_message=candidate_message.strip()[:500],
            action_hint=action_hint,
            company_name=self.company.name,
        )
        return await self.notify_supervisor(
            supervisor_open_id,
            text,
            position_id=position.id,
            candidate_id=candidate.id,
            kind=f"candidate_{intent}",
        )

    # ------------------------------------------------------------------ 留痕
    def _signature(self) -> str:
        """邮件署名。

        只保留公司名与「机器人自动发送」提示：联系人信息模板正文里已经有一行，
        署名再重复一次会让候选人看到两份同样的联系方式（实测截图中出现过）。
        """
        lines = [f"{self.company.name}｜HR 招聘组"]
        lines.append("本邮件由 ai-hr 招聘机器人自动发送，可直接回复本邮件。")
        return "\n".join(lines)

    async def _record_feishu_out(
        self,
        *,
        recipient: str,
        body: str,
        message_id: str,
        ok: bool,
        error: str = "",
        position_id: int | None = None,
        candidate_id: int | None = None,
        thread_key: str = "",
        parsed: dict | None = None,
    ) -> None:
        await self.messages.add(
            Message(
                channel=Channel.FEISHU,
                direction=Direction.OUTBOUND,
                sender="ai-hr-robot",
                recipient=recipient,
                body=body,
                external_id=message_id,
                thread_key=thread_key,
                candidate_id=candidate_id,
                position_id=position_id,
                parsed=parsed or {},
                status=("sent" if ok and self.settings.feishu_live else "mocked") if ok else "failed",
                error=error,
                sent_at=now_utc_naive(),
            )
        )

    async def _record_email_out(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        message_id: str,
        ok: bool,
        error: str = "",
        candidate_id: int | None = None,
        position_id: int | None = None,
        thread_key: str = "",
        status: str | None = None,
    ) -> None:
        await self.messages.add(
            Message(
                channel=Channel.EMAIL,
                direction=Direction.OUTBOUND,
                sender=self.settings.hr_mailbox,
                recipient=recipient,
                subject=subject,
                body=body,
                external_id=message_id,
                thread_key=thread_key,
                candidate_id=candidate_id,
                position_id=position_id,
                status=status
                or (("sent" if ok and self.settings.mail_live else "mocked") if ok else "failed"),
                error=error,
                sent_at=now_utc_naive(),
            )
        )


# --------------------------------------------------------------------- 工具


def _fmt_years(value: float | None) -> str:
    if value is None:
        return "0"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _deep_fields(candidate: Candidate) -> dict[str, str]:
    """把大模型深度解析压成摘要模板用的三行文本（没有就留空，模板自动跳过）。"""
    deep = (candidate.score_detail or {}).get("deep_analysis") or {}
    if not isinstance(deep, dict) or not deep:
        return {"deep_line": "", "risks": "", "interview_focus": ""}

    labels = deep.get("dimension_labels") or {}
    dims = deep.get("dimensions") or {}
    parts = []
    for key, score in dims.items():
        label = labels.get(key) or key
        parts.append(f"{label} {score:g}")
    line = "｜".join(parts)
    if deep.get("strengths"):
        line += f"；亮点：{'、'.join(deep['strengths'][:2])}"
    return {
        "deep_line": line,
        "risks": "、".join(deep.get("risks") or []),
        "interview_focus": "、".join(deep.get("interview_focus") or []),
    }


def markdown_to_html(text: str) -> str:
    """极简 Markdown → HTML（仅支持标题/加粗/列表/引用/段落），用于邮件正文。"""
    lines = text.split("\n")
    html_lines: list[str] = []
    in_list = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<br/>")
            continue
        if in_list and not stripped.startswith(("- ", "* ", "> ")) and not re.match(r"^\d+\.\s", stripped):
            html_lines.append("</ul>")
            in_list = False

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            content = html.escape(stripped[level:].strip())
            html_lines.append(f"<h{min(level, 6)}>{_inline(content)}</h{min(level, 6)}>")
        elif stripped.startswith(("- ", "* ")) or re.match(r"^\d+\.\s", stripped):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = stripped[2:] if stripped[:2] in {"- ", "* "} else re.sub(r"^\d+\.\s", "", stripped)
            html_lines.append(f"<li>{_inline(html.escape(content))}</li>")
        elif stripped.startswith("> "):
            html_lines.append(f"<blockquote>{_inline(html.escape(stripped[2:]))}</blockquote>")
        else:
            html_lines.append(f"<p>{_inline(html.escape(stripped))}</p>")

    if in_list:
        html_lines.append("</ul>")
    return "<html><body>" + "\n".join(html_lines) + "</body></html>"


def _inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)


__all__ = ["NotificationService", "NoticeResult", "markdown_to_html"]
