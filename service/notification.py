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
                logger.exception("dispatch.failed", extra=log_extra(position=position.code))
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

        feishu_text = await self.feishu.send_text(position.supervisor_open_id, summary_text)
        await self._record_feishu_out(
            recipient=position.supervisor_open_id,
            body=summary_text,
            message_id=feishu_text.message_id,
            ok=feishu_text.ok,
            error=feishu_text.error,
            position_id=position.id,
            parsed={"kind": "resume_package_notice", "passed_count": len(candidates)},
        )

        file_result = await self.feishu.send_file(
            position.supervisor_open_id, package_path, file_name=package_name
        )
        await self._record_feishu_out(
            recipient=position.supervisor_open_id,
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
        logger.info("package.built", extra=log_extra(position=position.code, path=str(package_path)))
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
    ) -> NoticeResult:
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
        message_id = self.mailbox.new_message_id()
        reply_to = self.mailbox.reply_address(token)

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
        else:
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
        await self.session.commit()
        return NoticeResult(ok=result.ok, message_id=message_id, detail=result.error)

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
        result = await self.feishu.send_text(open_id, text)
        await self._record_feishu_out(
            recipient=open_id,
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
                status=("sent" if ok and self.settings.mail_live else "mocked") if ok else "failed",
                error=error,
                sent_at=now_utc_naive(),
            )
        )


# --------------------------------------------------------------------- 工具


def _fmt_years(value: float | None) -> str:
    if value is None:
        return "0"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


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
