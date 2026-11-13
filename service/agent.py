"""飞书自然语言助手：把「主管说的人话」变成「查数据 + 说人话」。

三层分工，任何一层挂掉都不影响其余功能：

1. **路由**（``plan``）：模型只判断意图与槽位，输出 ``AgentPlan``；
   写操作的姓名必须落在当前名单里，否则丢弃（防模型编名字）。
2. **检索**（``_facts``）：按意图从数据库取真实数据，拼成紧凑 JSON。
3. **表达**（``_compose``）：模型只基于这份数据组织语言；模型不可用时降级成
   规则摘要（照样能回答，只是不口语化）。

写操作不在这里执行——``plan`` 判成 schedule/reschedule/confirm/cancel/reject 后，
由 ``FeedbackService`` 交给原有的确定性代码，模型不碰数据库。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from client.llm import LLMClient
from config.settings import Settings
from core.logging import get_logger, log_extra
from core.timeparse import format_interview_time, now_local
from model.candidate import Candidate, CandidateStatus
from model.event import Actor
from model.message import Channel, Message
from model.position import Position
from model.supervisor import Supervisor
from prompt.agent import ANSWER_SYSTEM, build_answer_prompt, build_router_prompt, ROUTER_SYSTEM
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from repository.message import MessageRepository
from repository.position import PositionRepository
from schema.agent import AgentFacts, AgentIntent, AgentPlan
from schema.config import CompanyInfo, MailboxConfig
from schema.message import InboundMessage
from service.notification import NotificationService

logger = get_logger(__name__)

_ABILITIES = (
    "1. 查候选人：名单、状态、分数、学历经验；\n"
    "2. 解释分数：这个分怎么算的、硬性条件过没过；\n"
    "3. 看统计：本轮收了多少、通过多少、淘汰多少；\n"
    "4. 读简历：某个人会不会某项技术、有没有某类项目经验；\n"
    "5. 安排面试：说「通知谁、什么时间」我就发面试通知；\n"
    "6. 改期/确认/取消：按你说的调整面试安排。"
)


@dataclass
class AgentReply:
    """Agent 的回答结果（由 FeedbackService 转成 FeedbackOutcome）。"""

    handled: bool = False
    action: str = ""
    text: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class AgentService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        llm: LLMClient | None,
        notification: NotificationService,
        company: CompanyInfo | None = None,
        mailbox_config: MailboxConfig | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.llm = llm
        self.notification = notification
        self.company = company or CompanyInfo()
        self.mailbox_config = mailbox_config or MailboxConfig()
        self.candidates = CandidateRepository(session)
        self.positions = PositionRepository(session)
        self.events = EventRepository(session)
        self.messages = MessageRepository(session)

    @property
    def enabled(self) -> bool:
        """模型可用才启用；否则整个 Agent 层静默退场，行为与改造前一致。"""
        return self.llm is not None and self.llm.enabled

    # ------------------------------------------------------------------ 路由
    async def plan(
        self,
        inbound: InboundMessage,
        *,
        positions: Sequence[Position],
        roster: Sequence[Candidate],
        pending_notices: int = 0,
        sender_title: str = "",
    ) -> AgentPlan | None:
        """让模型判断这句话要做什么。任何异常都返回 ``None``（调用方走原逻辑）。"""
        if not self.enabled:
            return None
        text = (inbound.text or "").strip()
        if not text:
            return None

        # 路由用的名单要比 active 宽：主管可能对「刚筛选通过、还没发给他」的人下指令
        # （典型是淘汰）。真正的执行限制仍在 FeedbackService 里（排期只认 roster）。
        pool: dict[int, Candidate] = {c.id: c for c in roster if c.id}
        for position in positions:
            for candidate in await self.candidates.list_by_position(position.id):
                pool.setdefault(candidate.id, candidate)
        pool_list = [c for c in pool.values() if c.status != CandidateStatus.SCREENED_REJECTED]

        roster_tuples = [
            (c.name or "", CandidateStatus.LABELS.get(c.status, c.status), float(c.score or 0.0))
            for c in pool_list
        ]
        history = await self._history(sender_id=inbound.sender_id, limit=6)
        prompt = build_router_prompt(
            text,
            now_local=now_local(self.settings.timezone),
            position_titles=[p.title for p in positions],
            roster=roster_tuples,
            pending_notices=pending_notices,
            history=history,
            sender_title=sender_title,
        )

        try:
            data = await self.llm.complete_json(  # type: ignore[union-attr]
                system=ROUTER_SYSTEM, user=prompt, temperature=0.0, max_tokens=1500
            )
        except Exception as exc:  # noqa: BLE001 - 路由失败不是错误，退回原逻辑即可
            logger.warning("agent.route_failed", extra=log_extra(error=f"{type(exc).__name__}: {exc}"))
            return None

        raw_names = data.get("names") or data.get("candidate_names") or []
        if isinstance(raw_names, str):
            raw_names = [chunk for chunk in raw_names.replace("，", ",").split(",") if chunk]
        plan = AgentPlan(
            intent=self._safe_intent(data.get("intent")),
            names=[str(n).strip() for n in raw_names if str(n).strip()],
            time_text=str(data.get("time_text") or data.get("time") or "").strip(),
            location=str(data.get("location") or "").strip(),
            question=str(data.get("question") or "").strip(),
            reason=str(data.get("reason") or "").strip(),
            method="llm",
            raw_intent=str(data.get("intent") or ""),
        )
        try:
            plan.confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            plan.confidence = 0.0

        # 姓名接地：写操作只能从「该主管职位下未淘汰的候选人」里选人。
        # roster 只包含可排期状态（dispatched 等），但「淘汰」可能针对刚筛选通过
        # 还没发主管的人，所以名单外的名字再查一次库确认。
        if plan.is_write:
            allowed = {c.name for c in pool_list if c.name}
            plan.names = [name for name in plan.names if name in allowed]

        logger.info(
            "agent.routed",
            extra=log_extra(
                intent=str(plan.intent),
                raw_intent=plan.raw_intent,
                names=plan.names,
                time_text=plan.time_text,
                confidence=plan.confidence,
            ),
        )
        return plan

    @staticmethod
    def _safe_intent(raw: Any) -> AgentIntent:
        from schema.agent import normalize_intent

        return normalize_intent(raw)

    # ------------------------------------------------------------------ 作答
    async def answer(
        self,
        inbound: InboundMessage,
        supervisor: Supervisor,
        plan: AgentPlan,
        *,
        positions: Sequence[Position],
        active: Sequence[Candidate],
    ) -> AgentReply:
        """读类意图：检索事实 → 模型组织语言 → 发飞书 → 留痕。"""
        facts = await self._facts(plan, positions=positions, active=active, sender_id=inbound.sender_id)
        text = await self._compose(plan, facts, supervisor, inbound.text)
        result = await self.notification.notify_supervisor(
            supervisor.feishu_open_id, text, kind=f"agent_{plan.intent}"
        )
        await self.events.record(
            "agent.answered",
            actor=Actor.ROBOT,
            object_type="supervisor",
            object_id=supervisor.id,
            detail={
                "intent": str(plan.intent),
                "question": (inbound.text or "")[:300],
                "facts_rows": len(facts.rows),
                "sent": result.ok,
                "error": result.detail[:200],
            },
            note=f"自然语言问答：{str(plan.intent)}",
        )
        return AgentReply(
            handled=True,
            action=f"agent_{plan.intent}",
            text=text,
            detail={"intent": str(plan.intent), "sent": result.ok, "rows": len(facts.rows)},
        )

    async def _compose(
        self,
        plan: AgentPlan,
        facts: AgentFacts,
        supervisor: Supervisor,
        original_text: str,
    ) -> str:
        question = plan.question or original_text or ""
        if self.enabled:
            try:
                text = await self.llm.complete_text(  # type: ignore[union-attr]
                    system=ANSWER_SYSTEM,
                    user=build_answer_prompt(
                        question=question,
                        facts_json=facts.as_prompt_json(),
                        sender_title=supervisor.title or supervisor.name,
                        company_name=self.company.name,
                        abilities=_ABILITIES
                        if plan.intent in (AgentIntent.CHAT, AgentIntent.UNKNOWN)
                        else "",
                    ),
                    temperature=0.3,
                    max_tokens=1200,
                )
                if text:
                    return text[:1500]
            except Exception as exc:  # noqa: BLE001 - 回答失败降级成数据摘要
                logger.warning(
                    "agent.answer_failed", extra=log_extra(error=f"{type(exc).__name__}: {exc}")
                )
        return self._fallback_text(plan, facts)

    def _fallback_text(self, plan: AgentPlan, facts: AgentFacts) -> str:
        """模型不可用时的规则摘要：宁可生硬，也不能不回。"""
        lines: list[str] = []
        if plan.intent == AgentIntent.CHAT:
            return "我这边可以帮您查候选人、解释筛选分数、看统计，也能直接安排面试。您想先看哪一项？"
        if facts.summary:
            lines.append(facts.summary)
        for row in facts.rows[:10]:
            parts = [str(row.get("姓名") or "")]
            for key in ("状态", "分数", "学历", "年限"):
                if row.get(key) not in (None, "", 0):
                    parts.append(f"{key} {row[key]}")
            lines.append("｜".join(part for part in parts if part))
        if facts.resume_excerpt:
            lines.append("简历片段：" + facts.resume_excerpt[:200].replace("\n", " "))
        if not lines:
            return "这条我暂时没查到对应数据，您可以换个说法，或者直接说要查的候选人姓名。"
        return "\n".join(lines)[:1200]

    # ------------------------------------------------------------------ 检索
    async def _facts(
        self,
        plan: AgentPlan,
        *,
        positions: Sequence[Position],
        active: Sequence[Candidate],
        sender_id: str,
    ) -> AgentFacts:
        """按意图检索事实。

        所有读意图都带两份「底稿」：候选人明细（含面试时间）与状态统计——
        主管经常一句话问两件事（「还剩几个人，王耀国定在什么时候」），
        只给一种数据模型就只能答一半。
        """
        facts = AgentFacts(history=await self._history(sender_id=sender_id, limit=6))
        position_ids = [p.id for p in positions]
        # 名单底稿用「该职位下所有未被淘汰的人」，而不是 active（active 只含可排期状态）。
        # 否则主管问「还剩几个人」会漏掉已筛选通过但还没发主管的那批。
        all_rows = await self._all_candidates(positions)
        alive = [c for c in all_rows if c.status != CandidateStatus.SCREENED_REJECTED]
        base_rows = [self._row(c) for c in alive[:15]]
        facts.summary = self._stats_text(all_rows)
        if alive:
            # 名单直接写进摘要：问「某个人」时模型也能顺带回答「一共还剩几个」
            facts.summary += "；未淘汰名单：" + "、".join(c.name for c in alive if c.name)

        if plan.intent in (
            AgentIntent.QUERY_CANDIDATES,
            AgentIntent.QUERY_STATS,
            AgentIntent.CHAT,
            AgentIntent.UNKNOWN,
        ):
            facts.rows = base_rows
            if plan.intent == AgentIntent.QUERY_CANDIDATES:
                facts.summary = f"当前在流程中的候选人 {len(active)} 人（按分数从高到低）"
            return facts

        # 以下都是「针对某个人」的意图
        targets = await self._resolve_targets(plan, active=active, position_ids=position_ids)
        facts.rows = [self._row(c, detail=plan.intent != AgentIntent.QUERY_RESUME) for c in targets[:5]]
        if not targets:
            facts.summary = "没有匹配到候选人" + (
                f"（查找的是：{'、'.join(plan.names)}）" if plan.names else ""
            )
            facts.rows = base_rows
            return facts

        if plan.intent == AgentIntent.QUERY_RESUME:
            # 只截第一个人的正文，避免一次塞太多 token
            facts.resume_excerpt = (targets[0].resume_text or "")[:2500]
            facts.summary = (
                f"候选人 {targets[0].name} 的简历摘要："
                f"{(targets[0].summary or '（无摘要）')[:200]}"
            )
        return facts

    async def _all_candidates(self, positions: Sequence[Position]) -> list[Candidate]:
        rows: list[Candidate] = []
        for position in positions:
            rows.extend(await self.candidates.list_by_position(position.id))
        return rows

    @staticmethod
    def _stats_text(all_rows: Sequence[Candidate]) -> str:
        """一行状态统计（所有读意图都会带上，便于回答顺带问到的数字）。"""
        if not all_rows:
            return "该主管名下还没有任何候选人记录"
        counter = Counter(CandidateStatus.LABELS.get(c.status, c.status) for c in all_rows)
        summary = f"累计 {len(all_rows)} 人：" + "；".join(
            f"{label} {count} 人" for label, count in counter.items()
        )
        cycles = Counter(str(c.cycle_id) for c in all_rows if c.cycle_id)
        if cycles:
            latest_cycle, count = cycles.most_common(1)[0]
            summary += f"；最近一轮（{latest_cycle}）{count} 人"
        return summary

    async def _resolve_targets(
        self,
        plan: AgentPlan,
        *,
        active: Sequence[Candidate],
        position_ids: Sequence[int],
    ) -> list[Candidate]:
        """点名优先；没点名就看最近进来的几个（避免模型凭空猜人）。"""
        if plan.names:
            hit = [c for c in active if c.name in plan.names]
            missing = [name for name in plan.names if name not in {c.name for c in hit}]
            if missing:
                # 已经不在流程里（比如已淘汰/已通知）的人也要能查到
                hit.extend(await self.candidates.list_by_names(missing, list(position_ids)))
            if hit:
                return hit
            # 名字没匹配上：按姓名模糊再试一次（主管可能只说了姓）
            found: list[Candidate] = []
            for name in plan.names:
                for position_id in position_ids:
                    rows = await self.candidates.list_by_position(position_id)
                    found.extend([c for c in rows if name in (c.name or "")])
            if found:
                return found
        return list(active[:3])

    def _row(self, candidate: Candidate, *, detail: bool = False) -> dict[str, Any]:
        """把候选人压成一行事实数据（字段名用中文，模型读起来更稳）。"""
        row: dict[str, Any] = {
            "姓名": candidate.name,
            "状态": CandidateStatus.LABELS.get(candidate.status, candidate.status),
            "分数": round(float(candidate.score or 0), 1),
            "职位": candidate.position_code,
            "学历": candidate.education_level,
            "院校": candidate.school,
            "专业": candidate.major,
            "年限": round(float(candidate.years_experience or 0), 1),
            "技能": list(candidate.skills or [])[:12],
        }
        if candidate.interview_time:
            row["面试时间"] = format_interview_time(candidate.interview_time, self.settings.timezone)
        else:
            # 显式写「未安排」，否则模型无法回答「谁还没安排面试」这类问题
            row["面试时间"] = "未安排"
        if candidate.email:
            row["邮箱"] = candidate.email
        if candidate.phone:
            row["电话"] = candidate.phone
        if detail:
            row.update(self._score_detail(candidate))
        return {key: value for key, value in row.items() if value not in (None, "", [], 0)}

    @staticmethod
    def _score_detail(candidate: Candidate) -> dict[str, Any]:
        """把 score_detail 里的关键结论摊平，方便模型解释分数。"""
        detail = candidate.score_detail or {}
        out: dict[str, Any] = {}
        if detail.get("reason"):
            out["筛选结论"] = str(detail["reason"])[:300]
        if detail.get("threshold"):
            out["阈值"] = detail["threshold"]
        hard_failed = detail.get("hard_failed") or candidate.hard_failed or []
        if hard_failed:
            out["未通过的硬条件"] = list(hard_failed)[:6]
        hard_unknown = detail.get("hard_unknown") or []
        if hard_unknown:
            out["字段缺失待复核"] = list(hard_unknown)[:6]
        hits = detail.get("hits") or []
        if hits:
            out["加权命中"] = [
                {
                    "项": hit.get("label") or hit.get("field"),
                    "实际": str(hit.get("actual"))[:80],
                    "得分率": round(float(hit.get("ratio") or 0), 2),
                }
                for hit in hits[:6]
                if isinstance(hit, dict)
            ]
        if detail.get("semantic_score") is not None:
            out["语义分"] = detail["semantic_score"]
            if detail.get("semantic_note"):
                out["语义分说明"] = str(detail["semantic_note"])[:200]
        deep = detail.get("deep_analysis") or {}
        if isinstance(deep, dict) and deep:
            if deep.get("strengths"):
                out["亮点"] = list(deep["strengths"])[:3]
            if deep.get("risks"):
                out["风险"] = list(deep["risks"])[:3]
        return out

    async def _history(self, *, sender_id: str = "", limit: int = 6) -> list[str]:
        """取最近几条飞书对话做上下文（群聊无法按人精确切分，按时间取即可）。"""
        if limit <= 0:
            return []
        stmt = (
            select(Message)
            .where(Message.channel == Channel.FEISHU)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())
        lines: list[str] = []
        for row in reversed(rows):
            who = "主管" if row.direction == "inbound" else "机器人"
            body = (row.body or "").replace("\n", " ").strip()
            if body:
                lines.append(f"{who}：{body[:200]}")
        return lines


__all__ = ["AgentReply", "AgentService"]
