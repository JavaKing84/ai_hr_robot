"""主管反馈解析：飞书自由文本、飞书卡片回调、原邮件回复三条入口汇到同一个用例。

两种回复路径（截图验收样例 + SKILL 要求）：
- 飞书里直接回复「我已经筛选审核完毕，请通知张三、李四，于下周三下午 14:00 来公司面试」
- 在收到的简历邮件上直接回复同样的内容

处理原则：
1. **只有主管给出明确结论（姓名 + 时间）才发通知邮件**；
2. 缺姓名或缺时间 → 回一条飞书消息问清楚，不猜、不发错邮件；
3. 候选人改期 → 先向主管确认新时间，确认后再补发确认邮件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from client.llm import LLMClient
from core.logging import get_logger, log_extra
from core.timeparse import format_interview_time, now_local, parse_chinese_datetime, to_utc_naive
from model.candidate import Candidate, CandidateStatus
from model.event import Actor
from model.message import Channel, Direction, Message
from model.position import Position
from model.supervisor import Supervisor, supervisor_from_position
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from repository.message import MessageRepository
from repository.position import PositionRepository
from schema.agent import AgentIntent, AgentPlan
from schema.config import CompanyInfo, MailboxConfig
from schema.extraction import InterviewScheduleSchema, SourceQuote
from schema.message import InboundMessage
from service.agent import AgentService
from service.extraction import ExtractionService
from service.notification import NotificationService

logger = get_logger(__name__)

_SCHEDULABLE_STATUSES = (
    CandidateStatus.DISPATCHED,
    CandidateStatus.SUPERVISOR_FEEDBACK,
    CandidateStatus.RESCHEDULE_PENDING,
    CandidateStatus.INTERVIEW_NOTIFIED,
)
_APPROVAL_WORDS = ("同意", "可以", "没问题", "行", "好的", "都行", "就这么定", "按这个时间")


def _years_label(candidate: Candidate) -> str:
    years = candidate.years_experience
    return f"{years} 年经验" if years not in (None, "", 0) else ""

# ---------------------------------------------------------------- 面试通知人工确认
# 机器人先把通知挂起，并在飞书里请示；HR 回复下面这些词才真正投递。
_CONFIRM_NOTICE_RE = re.compile(
    r"(确认发送|确认|确定|同意发送|同意|全部发送|都发|发送|发出去|发吧|可以发|send|approve)",
    re.IGNORECASE,
)
_CANCEL_NOTICE_RE = re.compile(
    r"(取消|不发|别发|先别发|不要发|作废|暂缓|cancel|reject)", re.IGNORECASE
)
# 带时间表达的回复说明是「安排面试」，不能当成对通知的确认
_TIME_EXPR_RE = re.compile(
    r"(\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*[点时]|周[一二三四五六日天]|星期[一二三四五六日天]"
    r"|明天|后天|今天|上午|下午|晚上|下周|本周)"
)

# ---------------------------------------------------------------- 岗位录入二次确认
# 「确认岗位 / 取消岗位」明确指岗位草稿；单独一个「确认」优先给面试通知（既有行为），
# 只有当时没有待发面试通知时才归岗位。详见 _handle_job_intake_decision。
_JOB_CONFIRM_RE = re.compile(
    r"(确认岗位|岗位确认|确认新增|确认修改|确认录入|录入吧|就这么录|就按这个来录)",
    re.IGNORECASE,
)
_JOB_CANCEL_RE = re.compile(
    r"(取消岗位|岗位取消|不录了|别录了|不要这个岗位|这个岗位算了|岗位作废)",
    re.IGNORECASE,
)



@dataclass
class FeedbackOutcome:
    handled: bool = False
    action: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    reply_text: str = ""


class FeedbackService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        extraction: ExtractionService,
        notification: NotificationService,
        company: CompanyInfo | None = None,
        mailbox_config: MailboxConfig | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.extraction = extraction
        self.notification = notification
        self.company = company or CompanyInfo()
        self.mailbox_config = mailbox_config or MailboxConfig()
        self.llm = llm
        self.candidates = CandidateRepository(session)
        self.positions = PositionRepository(session)
        self.events = EventRepository(session)
        self.messages = MessageRepository(session)
        #: 自然语言助手：LLM 不可用时 ``enabled=False``，所有路由调用直接返回 None，
        #: handle() 的行为与接入前完全一致（保持离线可用）。
        self.agent = AgentService(
            session,
            settings=settings,
            llm=llm,
            notification=notification,
            company=self.company,
            mailbox_config=self.mailbox_config,
        )

    # ------------------------------------------------------------------ 入口
    async def handle(self, inbound: InboundMessage) -> FeedbackOutcome:
        supervisor = await self._resolve_supervisor(inbound)
        # HR 本人不一定登记在 supervisors 表里（表里通常只有部门主管）：
        # 只要 FEISHU_OPEN_ID 对得上 HR 配置，就借用任一有职位的主管身份，
        # 这样 HR 在群里问「现在有哪些候选人」也能被正常回答。
        if supervisor is None and (self.settings.hr_feishu_open_id or "").strip() == inbound.sender_id:
            supervisor = await self._any_supervisor()
        if supervisor is None:
            await self.events.record(
                "feedback.supervisor_unknown",
                actor=Actor.SYSTEM,
                object_type="message",
                object_id=None,
                detail={"sender": inbound.sender_id, "channel": inbound.channel, "text": inbound.text[:200]},
                note="收到无法识别为主管的消息，已忽略",
            )
            await self.session.commit()
            return FeedbackOutcome(handled=False, action="supervisor_unknown", reply_text="发送者不是已登记的主管")

        positions = await self._positions_of(supervisor)
        if not positions:
            return FeedbackOutcome(handled=False, action="no_position", reply_text="该主管名下没有在招职位")

        message_row = await self._record_inbound(inbound, supervisor)

        # 场景 0：HR 在飞书里回复「确认 / 确认 1,3 / 取消」，放行或作废挂起的面试通知。
        # 放在最前面：这类回复既没有候选人名单也没有时间，交给排期解析会误判。
        pending_notices = await self.notification.pending_interview_notices()
        approval = await self._handle_interview_approval(
            inbound, supervisor, message_row, pending=pending_notices
        )
        if approval is not None:
            await self.session.commit()
            return approval

        # 场景 0b：HR 在飞书里回复「确认岗位 / 取消岗位」，让上面暂存的岗位草稿生效或作废。
        # 紧跟在面试审批之后、模型路由之前：这是确定性关键词判断，不该消耗一次模型调用，
        # 也不能让路由把「确认」解释成别的意图。
        job_decision = await self._handle_job_intake_decision(
            inbound, supervisor, message_row, pending_notices=len(pending_notices)
        )
        if job_decision is not None:
            await self.session.commit()
            return job_decision

        active = await self.candidates.list_active_for_positions(
            [p.id for p in positions], statuses=list(_SCHEDULABLE_STATUSES)
        )
        pending = [c for c in active if c.status == CandidateStatus.RESCHEDULE_PENDING]

        # 自然语言路由：读类由助手检索作答，写类仍走下面的确定性代码（模型只给名单与时间）。
        plan = await self.agent.plan(
            inbound,
            positions=positions,
            roster=active,
            pending_notices=len(pending_notices),
            sender_title=supervisor.title or supervisor.name,
        )

        if plan is not None and (plan.is_read or plan.intent == AgentIntent.UNKNOWN):
            # 读类意图直接由助手作答；UNKNOWN 也交给它——比固定模板更有用
            # （模型可以说「你是想问 X 吗」，而不是甩一段格式说明）
            reply = await self.agent.answer(
                inbound, supervisor, plan, positions=positions, active=active
            )
            await self.session.commit()
            return FeedbackOutcome(
                handled=reply.handled,
                action=reply.action,
                detail=reply.detail,
                reply_text=reply.text,
            )

        if plan is not None and plan.intent == AgentIntent.JOB_POSTING:
            outcome = await self._handle_job_posting(inbound, supervisor)
            await self.session.commit()
            return outcome

        if plan is not None and plan.intent == AgentIntent.SCREEN_TOP:
            outcome = await self._handle_screen_top(inbound, supervisor, positions, plan, message_row)
            await self.session.commit()
            return outcome

        if plan is not None and plan.intent == AgentIntent.REJECT:
            outcome = await self._handle_reject(
                inbound, supervisor, active, plan, message_row, positions
            )
            await self.session.commit()
            return outcome

        # 场景 A：主管在回复“候选人改期”的请示
        if pending and (plan is None or plan.intent == AgentIntent.RESCHEDULE):
            if self._looks_like_reschedule_decision(inbound.text, pending):
                outcome = await self._handle_reschedule_decision(
                    inbound, supervisor, positions, pending, message_row
                )
                await self.session.commit()
                return outcome

        # 场景 B：主管给出面试名单 + 时间（模型已抽出槽位时直接用，省一次调用）
        # RESCHEDULE（「王耀国改到 11月15日上午十点」）同样是一次「名单+时间」写入，
        # 槽位已经由路由阶段抽好，没有理由再调一次模型去猜。
        prefilled = (
            self._schedule_from_plan(plan)
            if plan is not None
            and plan.intent in (AgentIntent.SCHEDULE, AgentIntent.RESCHEDULE)
            else None
        )
        outcome = await self._handle_schedule(
            inbound, supervisor, positions, active, message_row, prefilled=prefilled
        )
        await self.session.commit()
        return outcome

    # ------------------------------------------- 场景 0：确认/取消面试通知投递
    async def _handle_interview_approval(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        message_row: Message,
        *,
        pending: Sequence[Message] | None = None,
    ) -> FeedbackOutcome | None:
        """处理 HR 对「待确认面试通知」的确认/取消回复。

        返回 ``None`` 表示这条消息不是审批指令（可能是名单+时间），继续走排期解析。
        支持：「确认」「确认发送」「发送」「确认 1,3」「取消」「取消 2」。
        """
        text = (inbound.text or "").strip()
        if not text:
            return None

        pending = list(pending) if pending is not None else await self.notification.pending_interview_notices()
        if not pending:
            return None

        # 带时间/日期表达的回复是「安排面试」，不当成对通知的答复
        if _TIME_EXPR_RE.search(text):
            return None

        # 「确认岗位 / 取消岗位」明确在说岗位草稿，不是答复面试通知。
        # 不在这里让路的话，「确认岗位」会被 _CONFIRM_NOTICE_RE 里的「确认」
        # 命中，于是把待发的面试通知全发了——HR 以为自己确认的是岗位。
        if _JOB_CONFIRM_RE.search(text) or _JOB_CANCEL_RE.search(text):
            return None

        cancel = _CANCEL_NOTICE_RE.search(text)
        confirm = _CONFIRM_NOTICE_RE.search(text)
        if not cancel and not confirm:
            return None

        # 序号对应请示消息里的编号（与 pending_interview_notices 的 id 顺序一致）
        numbers = [int(n) for n in re.findall(r"\d+", text)]
        targets: list[Message] = []
        for n in numbers:
            if 1 <= n <= len(pending):
                targets.append(pending[n - 1])
        if not targets:
            targets = list(pending)
        target_ids = [row.id for row in targets]
        # 回执文案要说清「他是在哪儿确认的」：从邮件回「确认」的人，看到
        # 「已在飞书确认」会以为自己点错了地方，甚至再回一遍。
        source = "邮件" if inbound.channel == Channel.EMAIL else "飞书"

        if cancel and not confirm:
            cancelled = await self.notification.cancel_pending_interview_notices(target_ids)
            reply = (
                f"已取消 {cancelled} 份待确认的面试通知，候选人不会收到任何邮件。\n"
                "需要重新安排的话，把名单和时间再发我一次即可。"
            )
            await self.notification.notify_supervisor(
                self._approval_open_id(supervisor),
                reply,
                kind="interview_approval_cancelled",
            )
            await self._reply_to_source(
                inbound, supervisor, reply, kind="interview_approval_cancelled"
            )
            await self.events.record(
                "interview.approval_cancelled",
                actor=Actor.HR,
                object_type="message",
                object_id=message_row.id,
                detail={
                    "cancelled": cancelled,
                    "message_ids": target_ids,
                    "text": text[:200],
                    "channel": inbound.channel,
                },
                note=f"HR 在{source}取消 {cancelled} 份面试通知（未投递）",
            )
            return FeedbackOutcome(
                handled=True,
                action="interview_notice_cancelled",
                detail={"cancelled": cancelled, "message_ids": target_ids},
                reply_text=reply,
            )

        results = await self.notification.confirm_pending_interview_notices(target_ids)
        ok = len([r for r in results if r.ok])
        failed = len(results) - ok
        # 演练模式（MAIL_MODE=mock）下邮件只写成 .eml 存本地，不会真的投递。
        # 此时若照旧回「已发送」，主管会以为候选人已经收到通知——这是最坏的一种
        # 假成功：库里状态是 interview_notified，候选人邮箱里却什么都没有。
        mocked = len([r for r in results if r.ok and getattr(r, "mode", "") == "mock"])
        if ok and mocked == ok:
            reply = (
                f"已处理 {ok} 份面试通知——当前是**演练模式**（MAIL_MODE=mock），"
                f"邮件只生成草稿存在 var/outbox/mail，**没有投递给候选人**。\n"
                "切回真实发送：把 .env 的 MAIL_MODE 改成 live 后重启服务。"
            )
        elif mocked:
            reply = (
                f"已发送 {ok} 份面试通知，其中 {mocked} 份为演练模式（仅存草稿、未投递）。"
            )
        else:
            reply = f"已按您在{source}里的确认，发送 {ok} 份面试通知。"
        if failed:
            reply += f"另有 {failed} 份发送失败，我会稍后重试。"
        await self.notification.notify_supervisor(
            self._approval_open_id(supervisor), reply, kind="interview_approval_sent"
        )
        await self._reply_to_source(inbound, supervisor, reply, kind="interview_approval_sent")
        await self.events.record(
            "interview.approval_confirmed",
            actor=Actor.HR,
            object_type="message",
            object_id=message_row.id,
            detail={
                "sent": ok,
                "failed": failed,
                "message_ids": target_ids,
                "text": text[:200],
                "channel": inbound.channel,
            },
            note=f"HR 在{source}确认发送面试通知：成功 {ok} 份、失败 {failed} 份",
        )
        return FeedbackOutcome(
            handled=True,
            action="interview_notice_confirmed",
            detail={"sent": ok, "failed": failed, "message_ids": target_ids},
            reply_text=reply,
        )

    def _approval_open_id(self, supervisor: Supervisor) -> str:
        """确认请示发给谁：配了 HR 本人 open_id 就发他，否则发触发安排的岗位主管。"""
        return (self.settings.hr_feishu_open_id or "").strip() or supervisor.feishu_open_id

    async def _reply_to_source(
        self, inbound: InboundMessage, supervisor: Supervisor, text: str, *, kind: str
    ) -> None:
        """回执回到「他说话的那个渠道」。

        飞书那条由 ``notify_supervisor`` 照发（相当于给对方留一份摘要），
        邮件来的指令再补一封邮件——否则主管在邮箱里操作完，邮箱里一片安静。
        """
        if inbound.channel != Channel.EMAIL or not supervisor.email:
            return
        await self.notification.notify_supervisor_by_email(supervisor.email, text, kind=kind)

    # ------------------------------------------------------------ 场景 B：安排
    async def _handle_schedule(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        positions: Sequence[Position],
        active: Sequence[Candidate],
        message_row: Message,
        *,
        prefilled: InterviewScheduleSchema | None = None,
    ) -> FeedbackOutcome:
        # 可排期名单含「已通知 / 改期中」：否则主管说「某某改到某时」会被当成看不懂。
        # 已通知的人重排＝改期，状态回 SUPERVISOR_FEEDBACK 并重走确认闸门。
        roster = [
            c
            for c in active
            if c.status
            in (
                CandidateStatus.DISPATCHED,
                CandidateStatus.SUPERVISOR_FEEDBACK,
                CandidateStatus.INTERVIEW_NOTIFIED,
                CandidateStatus.RESCHEDULE_PENDING,
            )
        ]
        roster_names = [c.name for c in roster if c.name]
        primary_position = self._primary_position(positions, roster)

        # 模型路由阶段已经抽好的槽位直接用，避免同一条消息调用两次大模型
        schedule = prefilled or self._schedule_from_card(inbound)
        if schedule is None:
            schedule = await self.extraction.extract_interview_schedule(
                inbound,
                roster=roster_names,
                position_title=primary_position.title if primary_position else "",
                location_default=self.mailbox_config.interview_location,
            )

        message_row.parsed = schedule.model_dump(mode="json")
        message_row.confidence = schedule.confidence
        await self.session.flush()

        names = [name for name in schedule.candidate_names if name in set(roster_names)]
        # 点名了但不在可排期名单里的人：绝不能静默丢掉。
        # 原来这里直接 filter，主管说「通知李四，下周三下午两点」而李四处于
        # screened_passed（还没发给主管）时，机器人既不排期、也不解释，
        # 主管以为安排好了——这是本项目最危险的一类「假成功」。
        requested = [name for name in schedule.candidate_names if name]
        if not requested:
            # 模型/规则都没抽出名字时，退一步在原文里找「主管名下确实存在的人名」。
            # 否则「名单里一个都没解析到」和「解析到了但不能排期」会被混成同一句
            # 通用格式提示，主管永远不知道问题出在人的状态上。
            requested = await self._names_mentioned(inbound.text, positions)
        else:
            # 模型只抽到一部分名字时（「【周涛，张三】」只抽出周涛），
            # 与它们并列、却没被抽出的词也要带出来——否则张三彻底消失，
            # 主管以为两个人都安排了。
            for extra in self._unknown_names_beside(inbound.text, requested):
                if extra not in requested:
                    requested.append(extra)
        missing = [name for name in requested if name not in set(roster_names)]
        missing_pairs = await self._unschedulable_pairs(missing, positions) if missing else []
        missing_note = self._format_unschedulable(missing_pairs)

        if not names:
            text = self._format_hint(supervisor, roster)
            if missing_note:
                text = f"{missing_note}\n\n{text}"
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id,
                text,
                position_id=primary_position.id if primary_position else None,
                kind="feedback_unparsed",
            )
            await self.events.record(
                "feedback.unparsed",
                actor=Actor.SUPERVISOR,
                object_type="message",
                object_id=message_row.id,
                detail={
                    "text": inbound.text[:500],
                    "roster": roster_names,
                    "unschedulable": missing,
                },
                note="未识别出可排期的面试名单，已回消息说明原因（含不可排期人员）",
            )
            return FeedbackOutcome(
                handled=False,
                action="unparsed",
                reply_text=text,
                detail={"unschedulable": missing},
            )

        if schedule.interview_time is None:
            text = (
                f"{supervisor.title or supervisor.name}，收到名单：{'、'.join(names)}。\n"
                "还差一个面试时间，请回复具体时间，例如「下周三下午 14:00」，我马上发面试通知邮件。"
            )
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id, text, kind="feedback_missing_time"
            )
            await self.events.record(
                "feedback.missing_time",
                actor=Actor.SUPERVISOR,
                object_type="message",
                object_id=message_row.id,
                detail={"names": names},
                note="已向主管追要面试时间（不猜时间，不发错邮件）",
            )
            return FeedbackOutcome(handled=False, action="missing_time", reply_text=text)

        interview_time = to_utc_naive(schedule.interview_time)
        location = (
            schedule.location
            or self.mailbox_config.interview_location
            or "地点待定"
        )

        selected = [c for c in roster if c.name in set(names)]
        grouped: dict[int, list[Candidate]] = {}
        for candidate in selected:
            grouped.setdefault(candidate.position_id or 0, []).append(candidate)

        sent_total = 0
        pending_total = 0
        summary_items: list[dict[str, object]] = []
        failed_items: list[dict[str, str]] = []
        for position_id, group in grouped.items():
            position = await self.session.get(Position, position_id)
            if position is None:
                continue
            for candidate in group:
                from_state = candidate.status
                # 已通知过的候选人再次被排期 = 改期，事件里留痕以便回溯
                is_reschedule = from_state in (
                    CandidateStatus.INTERVIEW_NOTIFIED,
                    CandidateStatus.RESCHEDULE_PENDING,
                )
                candidate.status = CandidateStatus.SUPERVISOR_FEEDBACK
                await self.session.flush()
                await self.events.record(
                    "feedback.received",
                    actor=Actor.SUPERVISOR,
                    object_type="candidate",
                    object_id=candidate.id,
                    from_state=from_state,
                    to_state=CandidateStatus.SUPERVISOR_FEEDBACK,
                    detail={
                        "interview_time": interview_time.isoformat(),
                        "location": location,
                        "source": inbound.channel,
                        "message_id": message_row.id,
                        "reschedule": is_reschedule,
                    },
                    note="主管改期" if is_reschedule else "主管确认面试名单与时间",
                )

            results = await self.notification.send_interview_notices(
                group, position, interview_time=interview_time, location=location
            )
            for candidate, result in zip(group, results):
                if result.pending:
                    pending_total += 1
                elif result.ok:
                    sent_total += 1
                if not result.ok and not result.pending:
                    failed_items.append(
                        {"name": candidate.name or "未命名", "reason": result.detail or "发送失败"}
                    )
                    continue
                # 时间逐人对：candidate.interview_time 是这一位自己落库的时间，
                # 不能拿第一条的时间套给整批（周涛「今天 15:00」曾被写成 11-18）。
                when = candidate.interview_time or interview_time
                summary_items.append(
                    {
                        "name": candidate.name or "未命名",
                        "position_title": position.title,
                        "time": format_interview_time(when, self.settings.timezone),
                        "score": f"{candidate.score:.1f}" if candidate.score else "",
                    }
                )

        # 只有「确认闸门开着」时才需要再问一句；闸门关掉后这里恒为 0，
        # 主管说完就直接排，不再让他确认第二遍。
        if pending_total:
            approval_title = supervisor.title or supervisor.name
            # 指令从哪个渠道来，请示就回哪个渠道：主管在邮箱里发的指令，
            # 飞书那条请示他看不到，面试通知会一直挂成待确认，看着像机器人没反应。
            if inbound.channel == Channel.EMAIL and supervisor.email:
                await self.notification.request_interview_approval_by_email(
                    supervisor.email, supervisor_title=approval_title
                )
            else:
                await self.notification.request_interview_approval(
                    self._approval_open_id(supervisor), supervisor_title=approval_title
                )

        # 排不了的人也要出现在同一条回执里，别让主管以为全都安排了。
        for name, reason in missing_pairs:
            failed_items.append({"name": name, "reason": reason})

        # 一条消息说清所有人 + 各自时间 + 地点，不按岗位拆成多条。
        if summary_items or failed_items:
            await self.notification.send_schedule_summary(
                supervisor.feishu_open_id,
                supervisor_title=supervisor.title or supervisor.name,
                items=summary_items,
                location=location,
                draft_mode=not self.settings.mail_live,
                failed=failed_items,
            )

        # 排不了的人已并进上面那条回执的 failed 段，不再单独补发第二条，
        # 否则主管一条指令收到两条消息、还得自己对哪条是结论。

        await self.events.record(
            "feedback.schedule_applied",
            actor=Actor.ROBOT,
            object_type="message",
            object_id=message_row.id,
            detail={
                "names": names,
                "interview_time": interview_time.isoformat(),
                "location": location,
                "notices_sent": sent_total,
                "notices_pending_confirm": pending_total,
                "source": inbound.channel,
            },
            note=(
                f"已按主管安排生成 {sent_total + pending_total} 份面试通知，"
                f"其中 {sent_total} 份已发送、{pending_total} 份待 HR 确认后发送"
            ),
        )
        return FeedbackOutcome(
            handled=True,
            action="interview_scheduled",
            detail={
                "candidate_names": names,
                "interview_time": interview_time.isoformat(),
                "location": location,
                "notices_sent": sent_total,
                "notices_pending_confirm": pending_total,
                "unschedulable": missing,
            },
        )

    async def handle_image(self, inbound: InboundMessage, image: bytes) -> FeedbackOutcome:
        """飞书发来一张图（JD 截图）的入口：认人 → 落消息 → 走招聘需求解析。"""
        supervisor = await self._resolve_supervisor(inbound)
        if supervisor is None and (self.settings.hr_feishu_open_id or "").strip() == inbound.sender_id:
            supervisor = await self._any_supervisor()
        if supervisor is None:
            return FeedbackOutcome(handled=False, action="supervisor_unknown", reply_text="发送者不是已登记的主管")
        await self._record_inbound(inbound, supervisor)
        outcome = await self._handle_job_posting(inbound, supervisor, image=image)
        await self.session.commit()
        return outcome

    # ------------------------------------------------- 场景 B1：主管发招聘需求
    async def _handle_job_posting(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        image: bytes | None = None,
    ) -> FeedbackOutcome:
        """主管发 JD（文字或截图）→ 解析成岗位配置，**先暂存**，等主管确认再生效。

        为什么不在这一步直接生效（2026-11-14 加）：模型偶尔会把「五年经验」抽成
        一条奇怪的硬条件，或把「改要求」抽成一个新岗位；直接落盘的话下一轮收简历
        就按错的规则筛，而 HR 以为自己只是「随口说了一句」。多一步确认，
        错的成本从「筛错一整批」降到「回一句取消」。
        """
        from service.job_intake import JobIntakeError, JobIntakeService, describe_pending

        intake = JobIntakeService(settings=self.settings, llm=self.llm)
        try:
            position = (
                await intake.parse_image(image)
                if image is not None
                else await intake.parse(inbound.text)
            )
        except JobIntakeError as exc:
            text = str(exc)
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id, text, kind="job_intake_failed"
            )
            return FeedbackOutcome(handled=False, action="job_intake_failed", reply_text=text)

        # 同 code 说明这是在改一个已有的岗位，确认文案要说「修改」而不是「新增」，
        # 否则主管会以为多了一个岗位，确认后发现旧的不见了。
        exists = any(p.code == position.code for p in self.settings.load_company_config().positions)
        path = intake.stage(position)
        # 模型偶发失败时结果是规则拼的，确认文案要如实说明（intake.last_source）
        text = describe_pending(position, exists=exists, source=intake.last_source)
        await self.notification.notify_supervisor(
            supervisor.feishu_open_id, text, kind="job_intake_confirm"
        )
        await self.events.record(
            "position.intake_staged",
            actor=Actor.SUPERVISOR,
            object_type="position",
            object_id=None,
            detail={
                "code": position.code,
                "title": position.title,
                "file": str(path),
                "replaces_existing": exists,
                "source": intake.last_source,
            },
            note=f"主管发来招聘需求，已暂存待确认：{position.title}",
        )
        return FeedbackOutcome(
            handled=True,
            action="job_intake_staged",
            detail={
                "code": position.code,
                "file": str(path),
                "replaces_existing": exists,
                "source": intake.last_source,
            },
            reply_text=text,
        )

    # --------------------------------------- 场景 B1b：岗位草稿的确认 / 作废
    async def _handle_job_intake_decision(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        message_row: Message,
        *,
        pending_notices: int = 0,
    ) -> FeedbackOutcome | None:
        """HR 回复「确认岗位 / 取消岗位」时，让暂存的草稿生效或作废。

        返回 ``None`` 表示这条消息不是岗位确认，交回原有流程。

        关键词优先级（刻意这样排，避免和面试通知的「确认」打架）：
        - 带「岗位」二字（确认岗位 / 取消岗位）→ 一律归岗位，优先级最高；
        - 单独一个「确认」→ 只有**当前没有待发面试通知**时才归岗位，
          否则保持既有行为（归面试通知），不动已跑通的审批路径。
        """
        from service.job_intake import JobIntakeService, describe

        text = (inbound.text or "").strip()
        if not text:
            return None

        intake = JobIntakeService(settings=self.settings, llm=self.llm)
        if not intake.staged_codes():
            return None  # 没有待确认的岗位，别抢别人的「确认」

        explicit_confirm = bool(_JOB_CONFIRM_RE.search(text))
        explicit_cancel = bool(_JOB_CANCEL_RE.search(text))
        if explicit_confirm:
            decision = "confirm"
        elif explicit_cancel:
            decision = "cancel"
        elif _CANCEL_NOTICE_RE.search(text):
            decision = "cancel"
        elif _CONFIRM_NOTICE_RE.search(text) and not pending_notices:
            decision = "confirm"
        else:
            return None

        # 待确认可能不止一份：只取最近一次录入的那份（HR 说「确认」时脑子里就是它）
        code = intake.latest_staged_code()
        position = intake.load_staged(code)
        if position is None:
            text = "那份待确认的岗位草稿我找不到了（可能已经处理过）。把要求再发我一遍，我重新确认一次。"
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id, text, kind="job_intake_missing"
            )
            return FeedbackOutcome(handled=False, action="job_intake_missing", reply_text=text)

        if decision == "cancel":
            intake.discard(code)
            text = f"已作废「{position.title}」，现有在招岗位一个都没动。"
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id, text, kind="job_intake_discarded"
            )
            await self.events.record(
                "position.intake_discarded",
                actor=Actor.HR,
                object_type="message",
                object_id=message_row.id,
                detail={"code": code, "title": position.title, "text": text[:200]},
                note=f"HR 作废了待确认的岗位草稿：{position.title}",
            )
            return FeedbackOutcome(
                handled=True, action="job_intake_discarded", detail={"code": code}, reply_text=text
            )

        # 确认：写正式草稿 → 立刻同步进数据库（HR 不用等下一轮才在职位列表里看到）
        path = intake.commit(position)
        await self.positions.sync_from_config(self.settings.load_company_config())
        text = describe(position)
        await self.notification.notify_supervisor(
            supervisor.feishu_open_id, text, kind="job_intake"
        )
        await self.events.record(
            "position.intaken",
            actor=Actor.HR,
            object_type="position",
            object_id=None,
            detail={"code": position.code, "title": position.title, "file": str(path)},
            note=f"HR 确认录入岗位：{position.title}",
        )
        return FeedbackOutcome(
            handled=True,
            action="job_intake",
            detail={"code": position.code, "file": str(path)},
            reply_text=text,
        )

    # ------------------------------------------------- 场景 B2：筛选前 N 份
    async def _handle_screen_top(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        positions: Sequence[Position],
        plan: AgentPlan,
        message_row: Message,
    ) -> FeedbackOutcome:
        """「筛 5 份」：按分数取前 N 份，回一份带简历链接的短名单。"""
        limit = min(max(plan.count or 5, 1), 20)
        # 已派发给主管的也算「可筛」：HR 常说「先给我挑 5 份」，此时简历早已发出去
        rows = await self.candidates.list_active_for_positions(
            [p.id for p in positions],
            statuses=[
                CandidateStatus.RECEIVED,
                CandidateStatus.SCREENED_PASSED,
                CandidateStatus.DISPATCHED,
                CandidateStatus.SUPERVISOR_FEEDBACK,
            ],
        )
        rows = sorted(rows, key=lambda c: c.score or 0, reverse=True)[:limit]

        if not rows:
            text = "现在没有待筛的简历，最近一批已经发给你了。"
            await self.notification.notify_supervisor(supervisor.feishu_open_id, text, kind="screen_empty")
            return FeedbackOutcome(handled=True, action="screen_empty", reply_text=text)

        base = self.settings.public_base_url.rstrip("/")
        lines = [f"挑出 {len(rows)} 份（按分数）："]
        for index, candidate in enumerate(rows, start=1):
            bits = "·".join(
                x for x in (candidate.education_level, candidate.school, _years_label(candidate)) if x
            )
            lines.append(f"{index}. {candidate.name} {round(candidate.score or 0, 1)}｜{bits}")
            lines.append(f"   {base}/api/v1/recruitment/candidates/{candidate.id}/resume")
        lines.append("看中谁，直接说「通知张三、李四，下周三下午两点」。")
        text = "\n".join(lines)

        for candidate in rows:
            if candidate.status == CandidateStatus.RECEIVED:
                candidate.status = CandidateStatus.SCREENED_PASSED
                await self.events.record(
                    "candidate.screened_on_demand",
                    actor=Actor.SUPERVISOR,
                    object_type="candidate",
                    object_id=candidate.id,
                    from_state=CandidateStatus.RECEIVED,
                    to_state=CandidateStatus.SCREENED_PASSED,
                    detail={"limit": limit, "source": inbound.channel},
                    note=f"按主管要求挑出前 {limit} 份",
                )
        await self.notification.notify_supervisor(
            supervisor.feishu_open_id, text, kind="screen_top"
        )
        return FeedbackOutcome(
            handled=True,
            action="screen_top",
            detail={"count": len(rows), "candidate_ids": [c.id for c in rows]},
            reply_text=text,
        )

    # ------------------------------------------------------- 场景 C：淘汰（自然语言）
    async def _handle_reject(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        active: Sequence[Candidate],
        plan: AgentPlan,
        message_row: Message,
        positions: Sequence[Position] = (),
    ) -> FeedbackOutcome:
        """主管说「张三不要了」——只改状态、记事件，不给候选人发任何邮件。

        产品口径：本项目不发感谢信/淘汰通知，所以这里绝不触发外发。
        「淘汰」可能针对刚筛选通过、还没发主管的人，所以候选池要覆盖
        该主管职位下所有未被淘汰的人，而不只是可排期状态。
        """
        pool: dict[int, Candidate] = {c.id: c for c in active}
        for position in positions:
            for candidate in await self.candidates.list_by_position(position.id):
                pool[candidate.id] = candidate
        candidates = [
            c for c in pool.values() if c.status != CandidateStatus.SCREENED_REJECTED
        ]
        known = {c.name for c in candidates if c.name}
        names = [name for name in plan.names if name in known]
        if not names:
            listing = "、".join(c.name for c in candidates[:8]) or "（当前没有候选人）"
            text = (
                f"{supervisor.title or supervisor.name}，要淘汰哪位？\n"
                f"当前未淘汰的候选人：{listing}\n"
                "直接回「淘汰张三」就行。"
            )
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id, text, kind="reject_need_name"
            )
            await self.events.record(
                "feedback.reject_unparsed",
                actor=Actor.SUPERVISOR,
                object_type="message",
                object_id=message_row.id,
                detail={"text": inbound.text[:200], "roster": sorted(known)},
                note="淘汰指令里没有匹配到名单内的人",
            )
            return FeedbackOutcome(handled=False, action="reject_need_name", reply_text=text)

        rejected: list[str] = []
        for candidate in candidates:
            if candidate.name not in names:
                continue
            from_state = candidate.status
            candidate.status = CandidateStatus.SCREENED_REJECTED
            await self.session.flush()
            await self.events.record(
                "candidate.rejected",
                actor=Actor.SUPERVISOR,
                object_type="candidate",
                object_id=candidate.id,
                from_state=from_state,
                to_state=CandidateStatus.SCREENED_REJECTED,
                detail={"by": inbound.sender_id, "channel": inbound.channel, "text": inbound.text[:200]},
                note="主管判定不合适，已从流程中移除（未通知候选人）",
            )
            rejected.append(candidate.name)

        reply = (
            f"已把 {'、'.join(rejected)} 标记为淘汰，不会再进入面试安排。\n"
            "按项目口径，系统不会给候选人发任何淘汰通知。"
        )
        await self.notification.notify_supervisor(
            supervisor.feishu_open_id, reply, kind="reject_applied"
        )
        await self.events.record(
            "feedback.reject_applied",
            actor=Actor.ROBOT,
            object_type="message",
            object_id=message_row.id,
            detail={"rejected": rejected},
            note=f"按主管指令淘汰 {len(rejected)} 人",
        )
        return FeedbackOutcome(
            handled=True,
            action="candidate_rejected",
            detail={"rejected": rejected},
            reply_text=reply,
        )

    def _schedule_from_plan(self, plan: AgentPlan) -> InterviewScheduleSchema | None:
        """把路由阶段抽到的槽位转成排期结构。

        时间原样交给 ``parse_chinese_datetime`` 解析（模型只负责抄原文，不换算日期）；
        解析不出来时 ``interview_time`` 为 None，下游会追问时间而不是瞎猜。
        """
        if not plan.names:
            return None
        parsed = (
            parse_chinese_datetime(
                plan.time_text, now=now_local(self.settings.timezone), tz=self.settings.timezone
            )
            if plan.time_text
            else None
        )
        return InterviewScheduleSchema(
            candidate_names=list(plan.names),
            interview_time=parsed,
            location=plan.location,
            confidence=plan.confidence or 0.6,
            method="llm",
            source_quotes=[
                SourceQuote(
                    field="candidate_names",
                    quote=plan.time_text or "、".join(plan.names),
                )
            ],
        )

    # ------------------------------------------------------------ 场景 A：改期
    def _looks_like_reschedule_decision(self, text: str, pending: Sequence[Candidate]) -> bool:
        names = [c.name for c in pending if c.name]
        if any(name in text for name in names):
            return True
        if parse_chinese_datetime(text, now=now_local(self.settings.timezone), tz=self.settings.timezone):
            return True
        return any(word in text for word in _APPROVAL_WORDS)

    async def _handle_reschedule_decision(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        positions: Sequence[Position],
        pending: Sequence[Candidate],
        message_row: Message,
    ) -> FeedbackOutcome:
        named = [c for c in pending if c.name and c.name in inbound.text]
        targets = named or list(pending)
        proposed = parse_chinese_datetime(
            inbound.text, now=now_local(self.settings.timezone), tz=self.settings.timezone
        )
        approved = any(word in inbound.text for word in _APPROVAL_WORDS)

        if proposed is None and not approved:
            text = (
                f"{supervisor.title or supervisor.name}，候选人 "
                f"{'、'.join(c.name for c in targets)} 申请改期。\n"
                "请直接回复新的时间（例如「改到周四下午 3 点」）或回复「同意」按候选人提议的时间安排。"
            )
            await self.notification.notify_supervisor(
                supervisor.feishu_open_id, text, kind="reschedule_need_decision"
            )
            return FeedbackOutcome(handled=False, action="reschedule_need_decision", reply_text=text)

        confirmed = 0
        applied: list[datetime] = []
        for candidate in targets:
            new_time = to_utc_naive(proposed) if proposed else await self._proposed_time(candidate)
            if new_time is None:
                continue
            applied.append(new_time)
            position = await self.session.get(Position, candidate.position_id)
            if position is None:
                continue
            old_time = candidate.interview_time
            result = await self.notification.send_reschedule_confirmation(
                candidate,
                position,
                new_time=new_time,
                old_time=old_time,
                location=candidate.interview_location or self.mailbox_config.interview_location,
            )
            if result.ok:
                confirmed += 1
            await self.events.record(
                "candidate.reschedule_confirmed",
                actor=Actor.ROBOT,
                object_type="candidate",
                object_id=candidate.id,
                from_state=CandidateStatus.RESCHEDULE_PENDING,
                to_state=CandidateStatus.INTERVIEW_NOTIFIED,
                detail={
                    "new_time": new_time.isoformat(),
                    "old_time": old_time.isoformat() if old_time else None,
                    "approved_by_supervisor": True,
                },
                note="主管确认改期，已补发确认邮件",
            )

        await self.notification.notify_supervisor(
            supervisor.feishu_open_id,
            (
                f"{supervisor.title or supervisor.name}，已按您的时间调整并通知候选人："
                f"{'、'.join(c.name for c in targets)}"
                f"（{format_interview_time(applied[0] if applied else None, self.settings.timezone)}）。"
            ),
            kind="reschedule_confirmed_receipt",
        )
        return FeedbackOutcome(
            handled=True,
            action="reschedule_confirmed",
            detail={"candidates": [c.name for c in targets], "confirmed": confirmed},
        )

    # ------------------------------------------------------------------ 工具
    async def _proposed_time(self, candidate: Candidate) -> datetime | None:
        events = await self.events.list_for_object("candidate", candidate.id)
        for event in reversed(events):
            if event.action == "candidate.reply.reschedule":
                raw = (event.detail or {}).get("proposed_time")
                if raw:
                    try:
                        return datetime.fromisoformat(raw)
                    except ValueError:
                        continue
        return None

    def _schedule_from_card(self, inbound: InboundMessage) -> InterviewScheduleSchema | None:
        if inbound.kind != "card_action" or not inbound.action:
            return None
        names = inbound.action.get("candidate_names") or inbound.action.get("names") or []
        if isinstance(names, str):
            names = [chunk for chunk in names.replace("，", ",").split(",") if chunk]
        raw_time = inbound.action.get("interview_time") or inbound.action.get("time")
        parsed_time = (
            parse_chinese_datetime(str(raw_time), now=now_local(self.settings.timezone), tz=self.settings.timezone)
            if raw_time
            else None
        )
        if not names and not parsed_time:
            return None
        return InterviewScheduleSchema(
            candidate_names=[str(name) for name in names],
            interview_time=parsed_time,
            location=str(inbound.action.get("location") or ""),
            confidence=0.95,
            source_quotes=[SourceQuote(field="card_action", quote=str(inbound.action)[:200])],
            method="rule",
        )

    async def _names_mentioned(
        self, text: str, positions: Sequence[Position]
    ) -> list[str]:
        """从主管原话里找人名：库里认得的 + 与他并列但库里没有的。

        只在模型与规则都没抽出结构化名单时才兜底：主管写「把李四挪到下周」
        这类没有标准句式的句子时，也要能用真实姓名把「他为什么排不了」
        讲清楚，而不是回一句通用格式提示让人自己猜。

        第二个来源（``_unknown_names_beside``）是 2026-11-14 补的：主管写
        「【周涛，张三】今天下午 3 点」时，模型只抽出周涛，张三就彻底消失——
        主管以为两个都安排了。与已识别人名**并列出现**的词要带出来，
        让上层回一句「张三我这边没有这个人」。
        """
        if not text:
            return []
        position_ids = [p.id for p in positions if p.id]
        if not position_ids:
            return []
        result = await self.session.execute(
            select(Candidate.name).where(
                Candidate.position_id.in_(position_ids), Candidate.name != ""
            )
        )
        known = {name for name in result.scalars().all() if name}
        if not known:
            return []
        # 长名字优先：「张三丰」命中后不再重复计一个「张三」
        hits = [name for name in sorted(known, key=len, reverse=True) if name in text]
        if not hits:
            return []
        return hits + self._unknown_names_beside(text, hits)

    #: 并列片段里出现这些词时不算人名（避免把「下午三点」当成候选人）
    _NOT_A_NAME = frozenset(
        {
            "今天", "明天", "后天", "昨天", "下周", "本周", "这周", "下月",
            "上午", "下午", "晚上", "中午", "早上", "时间", "地点", "面试",
            "通知", "安排", "可以", "即可", "我们", "他们", "你们", "简历",
            "候选", "名单", "麻烦", "辛苦", "谢谢", "收到",
        }
    )

    #: 剥掉已知人名后，残留词只要含这些字就判定不是姓名（多为动词/介词）
    _NON_NAME_CHARS = frozenset("请于在把给让和跟到来去的了就还再又都也才被将对于与及或但是")

    @classmethod
    def _unknown_names_beside(cls, text: str, known_hits: Sequence[str]) -> list[str]:
        """找出与已识别人名并列、但库里没有的姓名片段。

        只收「同一并列组里已经有认得的人」这种高置信场景：
        「【周涛，张三】」→ 周涛认得 → 张三收录；
        「通知赵敏，陈静下周三下午两点」→ 剥掉赵敏后剩「通知」，不是人名，丢弃。
        """
        if not text or not known_hits:
            return []

        # 1) 括号里的并列名单最可靠，先处理
        groups: list[list[str]] = []
        for inner in re.findall(r"[【\[（(]([^】\]）)]+)[】\]）)]", text):
            groups.append(re.split(r"[，,、/和及与\s]+", inner))
        # 2) 整句按分隔符切，剥掉已识别人名后看剩什么
        groups.append(re.split(r"[，,、/和及与\s]+", text))

        extras: list[str] = []
        for segments in groups:
            cleaned = [seg.strip(" 　.。:：;；!！?？【】[]（）()") for seg in segments]
            if not any(
                any(name in seg for name in known_hits) for seg in cleaned if seg
            ):
                # 这一组里没有认得的人，说明不是并列名单，不敢猜
                continue
            for seg in cleaned:
                if not seg:
                    continue
                rest = seg
                for name in known_hits:
                    rest = rest.replace(name, "")
                rest = rest.strip(" 　.。:：;；!！?？")
                if not (2 <= len(rest) <= 4) or not re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", rest):
                    continue
                if rest in cls._NOT_A_NAME or rest in extras:
                    continue
                if cls._NON_NAME_CHARS & set(rest):
                    continue
                extras.append(rest)
        return extras

    async def _unschedulable_pairs(
        self, names: Sequence[str], positions: Sequence[Position]
    ) -> list[tuple[str, str]]:
        """点名的人不在可排期名单里时，逐人给出（姓名, 原因）。

        查库范围是**该主管名下全部职位**（不限状态），所以能覆盖：
        刚筛选通过还没发主管、已淘汰、已放弃、已确认参加等情况；
        库里完全没有这个名字时也明说，而不是当作没看见。

        返回结构化而不是拼好的段落：调用方要把它并进「一条回执」的失败段，
        自己拼字符串就只能再发第二条消息。
        """
        if not names:
            return []
        position_ids = [p.id for p in positions if p.id]
        found: dict[str, Candidate] = {}
        if position_ids:
            rows = await self.candidates.list_by_names(names, position_ids)
            for candidate in rows:
                if candidate.name:
                    found.setdefault(candidate.name, candidate)

        reasons = {
            CandidateStatus.RECEIVED: "刚收到、还没筛选，暂时不能排期",
            CandidateStatus.PARSED: "刚解析完、还没筛选，暂时不能排期",
            CandidateStatus.SCREENED_PASSED: "筛选通过但还没发给主管，暂时不能排期",
            CandidateStatus.SCREENED_REJECTED: "已被判定不合适（已淘汰）",
            CandidateStatus.DECLINED: "已放弃面试",
            CandidateStatus.COMPLETED: "已确认参加，如需改期请直接说新时间",
            CandidateStatus.INTERVIEW_PENDING_CONFIRM: "面试通知待 HR 确认，暂不重复安排",
        }

        pairs: list[tuple[str, str]] = []
        for name in names:
            candidate = found.get(name)
            if candidate is None:
                pairs.append((name, "我这边没有这个人（请确认姓名，或该候选人不在您名下职位）"))
                continue
            state = CandidateStatus.LABELS.get(candidate.status, candidate.status)
            reason = reasons.get(candidate.status, "当前状态不可排期")
            pairs.append((name, f"{state}——{reason}"))
        return pairs

    @staticmethod
    def _format_unschedulable(pairs: Sequence[tuple[str, str]]) -> str:
        if not pairs:
            return ""
        return "这几位这次没能安排：" + "\n" + "\n".join(f"· {n}：{r}" for n, r in pairs)

    async def _explain_unschedulable(
        self, names: Sequence[str], positions: Sequence[Position]
    ) -> str:
        """（兼容入口）返回拼好的说明段落。新代码请用 ``_unschedulable_pairs``。"""
        return self._format_unschedulable(await self._unschedulable_pairs(names, positions))

    async def _resolve_supervisor(self, inbound: InboundMessage) -> Supervisor | None:
        """飞书按 open_id 认人，邮件按发件地址认人——两条路径都落到同一张 supervisors 表。"""
        if inbound.channel == Channel.FEISHU:
            return await self._supervisor_by_open_id(inbound.sender_id)
        return await self._supervisor_by_email(inbound.sender_id)

    async def _supervisor_by_open_id(self, open_id: str) -> Supervisor | None:
        result = await self.session.execute(
            select(Supervisor).where(Supervisor.feishu_open_id == open_id)
        )
        return result.scalars().first()

    async def _supervisor_by_email(self, email: str) -> Supervisor | None:
        address = (email or "").strip().lower()
        if not address:
            return None
        result = await self.session.execute(
            select(Supervisor).where(Supervisor.email.ilike(address))
        )
        found = result.scalars().first()
        if found is not None:
            return found
        # 兜底：supervisors 表按飞书号存人，多个主管共用同一个飞书号时会被挤成一行，
        # 表里只剩最后同步的那位。邮件认人因此必须回落到职位配置——否则张伟/李娜
        # 从自己邮箱回复，走到这里就被判成「不是已登记的主管」，回信静默消失。
        rows = await self.session.execute(
            select(Position)
            .where(Position.supervisor_email.ilike(address), Position.active.is_(True))
            .order_by(Position.id)
        )
        position = rows.scalars().first()
        if position is None:
            return None
        logger.info(
            "feedback.supervisor_from_position",
            extra=log_extra(email=address, position=position.code, name=position.supervisor_name),
        )
        return supervisor_from_position(position)

    async def _positions_of(self, supervisor: Supervisor) -> list[Position]:
        result = await self.session.execute(
            select(Position).where(
                Position.supervisor_open_id == supervisor.feishu_open_id, Position.active.is_(True)
            )
        )
        return list(result.scalars().all())

    async def _any_supervisor(self) -> Supervisor | None:
        """取一个名下有在招职位的主管，作为「HR 本人未登记」时的审批身份。"""
        result = await self.session.execute(select(Supervisor).order_by(Supervisor.id))
        for candidate_supervisor in result.scalars().all():
            if await self._positions_of(candidate_supervisor):
                return candidate_supervisor
        return None

    @staticmethod
    def _primary_position(positions: Sequence[Position], roster: Sequence[Candidate]) -> Position | None:
        if len(positions) == 1:
            return positions[0]
        if roster:
            position_id = roster[0].position_id
            for position in positions:
                if position.id == position_id:
                    return position
        return positions[0] if positions else None

    def _format_hint(self, supervisor: Supervisor, roster: Sequence[Candidate]) -> str:
        names = "、".join(c.name for c in roster[:8]) or "（本轮没有待反馈的候选人）"
        return (
            f"{supervisor.title or supervisor.name}，我还没看懂您的安排，请按下面任一种格式回复我：\n"
            f"「请通知张三、李四，下周三下午 14:00 来公司面试」\n\n"
            f"本轮待您反馈的候选人：{names}\n"
            "如果全部通过，直接回复「全部通过，下周三下午 14:00 面试」也可以。"
        )

    async def _record_inbound(self, inbound: InboundMessage, supervisor: Supervisor) -> Message:
        message = await self.messages.add(
            Message(
                channel=Channel.FEISHU if inbound.channel == "feishu" else Channel.EMAIL,
                direction=Direction.INBOUND,
                sender=inbound.sender_id,
                recipient="ai-hr-robot" if inbound.channel == "feishu" else self.settings.hr_mailbox,
                subject=inbound.subject,
                body=inbound.text,
                external_id=inbound.message_id,
                thread_key=inbound.thread_id,
                supervisor_id=supervisor.id,
                status="received",
                received_at=inbound.received_at,
                raw={"kind": inbound.kind, "action": inbound.action},
            )
        )
        return message


__all__ = ["FeedbackOutcome", "FeedbackService"]
