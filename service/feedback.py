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
from model.supervisor import Supervisor
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

        active = await self.candidates.list_active_for_positions(
            [p.id for p in positions], statuses=list(_SCHEDULABLE_STATUSES)
        )
        pending = [c for c in active if c.status == CandidateStatus.RESCHEDULE_PENDING]

        # 场景 1（2026-11-13 新增）：自然语言路由。
        # 模型只判断「这句话是问还是做」：
        # - 读类 → 检索真实数据后由模型组织人话回答（不碰数据库）
        # - 写类 → 仍交给下面的确定性代码执行（模型只提供名单与时间槽位）
        # 模型不可用时 plan 恒为 None，下面的分支与改造前完全一致。
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
            await self.events.record(
                "interview.approval_cancelled",
                actor=Actor.HR,
                object_type="message",
                object_id=message_row.id,
                detail={"cancelled": cancelled, "message_ids": target_ids, "text": text[:200]},
                note=f"HR 在飞书取消 {cancelled} 份面试通知（未投递）",
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
        reply = f"已按您的确认发送 {ok} 份面试通知。"
        if failed:
            reply += f"另有 {failed} 份发送失败，我会稍后重试。"
        await self.notification.notify_supervisor(
            self._approval_open_id(supervisor), reply, kind="interview_approval_sent"
        )
        await self.events.record(
            "interview.approval_confirmed",
            actor=Actor.HR,
            object_type="message",
            object_id=message_row.id,
            detail={"sent": ok, "failed": failed, "message_ids": target_ids, "text": text[:200]},
            note=f"HR 在飞书确认发送面试通知：成功 {ok} 份、失败 {failed} 份",
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
        # 可排期名单：除了「刚派发 / 已反馈」的人，还要包含「已通知面试」与
        # 「候选人申请改期」——否则主管说「王耀国改到 11月15日上午十点」时，
        # 这个人不在名单里，就会被当成看不懂而回一段格式说明（2026-11-13 修）。
        # 已通知的人重排＝改期：状态回到 SUPERVISOR_FEEDBACK 并重新走一次
        # 通知确认闸门，不会直接给候选人发第二封邮件。
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
        if not names:
            text = self._format_hint(supervisor, roster)
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
                detail={"text": inbound.text[:500], "roster": roster_names},
                note="未识别出面试名单，已回消息说明格式",
            )
            return FeedbackOutcome(handled=False, action="unparsed", reply_text=text)

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
        receipts: list[tuple[Position, list[Candidate]]] = []
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
            sent_total += len([r for r in results if r.ok and not r.pending])
            pending_total += len([r for r in results if r.pending])
            receipts.append((position, list(group)))

        # 有待确认的通知：回执换成「请 HR 确认是否发送」的飞书请示，
        # 在 HR 回复「确认」之前一封都不会投递。
        if pending_total:
            await self.notification.request_interview_approval(
                self._approval_open_id(supervisor),
                supervisor_title=supervisor.title or supervisor.name,
            )
        if sent_total:
            for position, group in receipts:
                await self.notification.send_receipt_to_supervisor(
                    supervisor.feishu_open_id,
                    supervisor_title=supervisor.title or supervisor.name,
                    position=position,
                    candidates=group,
                    interview_time=interview_time,
                    location=location,
                )

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
            },
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
        result = await self.session.execute(
            select(Supervisor).where(Supervisor.email.ilike((email or "").strip().lower()))
        )
        return result.scalars().first()

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
