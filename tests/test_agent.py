"""飞书自然语言助手（``service/agent.py``）测试。

这一层是「主管说一句人话 → 查真实数据 → 说人话回」的实现，出错的表现都很隐蔽：

- 模型挂了还在等人回话（应该静默退回原逻辑，不能让主管觉得机器人死了）；
- 模型编了个不存在的名字，代码照着去排面试（必须把名单外的名字丢掉）；
- 回答里的数字和数据库对不上（事实检索错了，模型会把错的数字说得很有说服力）；
- 群里三个主管同时说话，上下文串台（2026-11-14 修过，这里钉住）。

所以文件按四块组织：路由（plan）→ 作答（answer）→ 检索（_facts / _resolve_targets）
→ 上下文（_history）。不连真实模型、不发真实飞书，全部用 stub。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from model.candidate import Candidate, CandidateStatus
from model.message import Channel, Direction, Message
from model.position import Position
from model.supervisor import Supervisor
from schema.agent import AgentIntent, AgentPlan
from schema.message import InboundMessage
from service.agent import AgentService, _safe_int

SUPERVISOR_OPEN_ID = "ou_mock_zhangwei"


# --------------------------------------------------------------------- 测试替身


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StubLLM:
    """替身模型：只实现 agent 用到的两个方法，并记录收到的提示词。"""

    enabled = True

    def __init__(
        self,
        payload: Any = None,
        *,
        text: str = "",
        json_error: Exception | None = None,
        text_error: Exception | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {}
        self.text = text
        self.json_error = json_error
        self.text_error = text_error
        self.json_prompts: list[dict[str, Any]] = []
        self.text_prompts: list[dict[str, Any]] = []

    async def complete_json(self, **kwargs: Any):
        self.json_prompts.append(kwargs)
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    async def complete_text(self, **kwargs: Any) -> str:
        self.text_prompts.append(kwargs)
        if self.text_error is not None:
            raise self.text_error
        return self.text

    @property
    def last_user_prompt(self) -> str:
        return self.json_prompts[-1]["user"] if self.json_prompts else ""


class DisabledLLM:
    """``LLM_MODE=off`` 时的真身行为：enabled=False，方法绝不该被调用。"""

    enabled = False

    async def complete_json(self, **kwargs: Any):  # pragma: no cover - 被调就是 bug
        raise AssertionError("模型关闭时不应该调用 complete_json")

    async def complete_text(self, **kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("模型关闭时不应该调用 complete_text")


def _inbound(text: str = "", *, sender_id: str = SUPERVISOR_OPEN_ID) -> InboundMessage:
    return InboundMessage(channel="feishu", sender_id=sender_id, text=text, received_at=_now())


def _plan(intent: AgentIntent, names: list[str] | None = None, question: str = "") -> AgentPlan:
    return AgentPlan(intent=intent, names=names or [], question=question)


def _agent(services, llm=None) -> AgentService:
    company = services.container.company_config
    return AgentService(
        services.session,
        settings=services.container.settings,
        llm=llm,
        notification=services.notification,
        company=company.company,
        mailbox_config=company.mailbox,
    )


async def _candidate(
    session,
    position: Position,
    name: str = "张明",
    *,
    status: str = CandidateStatus.DISPATCHED,
    score: float = 88.0,
    email: str | None = None,
    **extra: Any,
) -> Candidate:
    candidate = Candidate(
        name=name,
        # 用字符编码拼一个稳定邮箱：不要用内置 hash()，它跨进程不稳定（项目里踩过）
        email=email or f"c{sum(ord(ch) for ch in name)}@example.com",
        phone="13800001111",
        position_id=position.id,
        position_code=position.code,
        status=status,
        score=score,
        **extra,
    )
    session.add(candidate)
    await session.flush()
    return candidate


@pytest.fixture
async def positions(services) -> list[Position]:
    rows = await services.session.execute(select(Position).where(Position.department == "技术部"))
    return list(rows.scalars().all())


@pytest.fixture
async def backend_position(positions: list[Position]) -> Position:
    return next(p for p in positions if p.code == "backend_senior")


@pytest.fixture
async def supervisor(services) -> Supervisor:
    row = await services.session.execute(
        select(Supervisor).where(Supervisor.feishu_open_id == SUPERVISOR_OPEN_ID)
    )
    return row.scalars().one()


# ------------------------------------------------------------------ 路由：兜底优先


async def test_plan_returns_none_when_llm_is_disabled(services, positions) -> None:
    """模型关闭时整层静默退场，调用方继续走原来的规则逻辑。"""
    agent = _agent(services, llm=DisabledLLM())

    assert agent.enabled is False
    assert await agent.plan(_inbound("今天收了多少份"), positions=positions, roster=[]) is None


async def test_plan_returns_none_without_llm(services, positions) -> None:
    """没配模型（llm=None）是最常见的部署状态，不能抛 AttributeError。"""
    assert await _agent(services, llm=None).plan(_inbound("有哪些人"), positions=positions, roster=[]) is None


async def test_plan_returns_none_for_blank_text(services, positions) -> None:
    """空白消息不值得花一次模型调用，也避免把空问题当成「查全部」。"""
    llm = StubLLM({"intent": "query_candidates"})
    agent = _agent(services, llm=llm)

    assert await agent.plan(_inbound("   \n "), positions=positions, roster=[]) is None
    assert llm.json_prompts == []


async def test_plan_returns_none_when_llm_raises(services, positions) -> None:
    """模型超时/限流不能让主管收不到回复，异常必须内部吞掉。"""
    llm = StubLLM(json_error=TimeoutError("模拟超时"))

    assert await _agent(services, llm=llm).plan(_inbound("有哪些人"), positions=positions, roster=[]) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("安排面试", AgentIntent.SCHEDULE),
        ("schedule", AgentIntent.SCHEDULE),
        ("安排面试(schedule)", AgentIntent.SCHEDULE),
        ("统计", AgentIntent.QUERY_STATS),
        ("你能干什么", AgentIntent.CHAT),
        ("完全看不懂的意图", AgentIntent.UNKNOWN),
        ("", AgentIntent.UNKNOWN),
        (None, AgentIntent.UNKNOWN),
    ],
)
async def test_plan_normalizes_intent_writing(services, positions, raw, expected) -> None:
    """国内小模型常回中文意图名，必须归一化，否则消息会被判成「没听懂」。"""
    llm = StubLLM({"intent": raw})

    plan = await _agent(services, llm=llm).plan(_inbound("随便说一句"), positions=positions, roster=[])

    assert plan is not None
    assert plan.intent == expected
    assert plan.method == "llm"


# ------------------------------------------------------ 路由：姓名接地（防模型编人）


async def test_write_intent_drops_names_outside_roster(services, positions, backend_position) -> None:
    """写操作只能对真实存在的人下手；模型编出来的名字必须被丢掉。"""
    await _candidate(services.session, backend_position, "张明")
    llm = StubLLM({"intent": "schedule", "names": ["张明", "编造的名字"], "time_text": "后天上午十点"})

    plan = await _agent(services, llm=llm).plan(_inbound("安排一下面试"), positions=positions, roster=[])

    assert plan is not None
    assert plan.names == ["张明"]


async def test_write_intent_with_only_fake_names_ends_up_empty(services, positions) -> None:
    """名单里一个人都没有时，模型点谁都不算——执行层据此回「你说的是谁」。"""
    llm = StubLLM({"intent": "reject", "names": ["张三丰"]})

    plan = await _agent(services, llm=llm).plan(_inbound("淘汰张三丰"), positions=positions, roster=[])

    assert plan is not None
    assert plan.names == []


async def test_read_intent_keeps_unknown_names(services, positions) -> None:
    """查询意图不接地：主管可能问一个已淘汰/已入职的人，答「没查到」才是对的。"""
    llm = StubLLM({"intent": "query_candidate", "names": ["已离职的老王"]})

    plan = await _agent(services, llm=llm).plan(_inbound("老王现在什么情况"), positions=positions, roster=[])

    assert plan is not None
    assert plan.names == ["已离职的老王"]


async def test_router_prompt_includes_undispatched_but_not_rejected(
    services, positions, backend_position
) -> None:
    """路由名单要比 active 宽：刚筛选通过、还没发给主管的人也能被点名淘汰。"""
    await _candidate(services.session, backend_position, "待发主管", status=CandidateStatus.SCREENED_PASSED)
    await _candidate(services.session, backend_position, "已淘汰的", status=CandidateStatus.SCREENED_REJECTED)
    llm = StubLLM({"intent": "query_candidates"})

    await _agent(services, llm=llm).plan(_inbound("都有谁"), positions=positions, roster=[])

    prompt = llm.last_user_prompt
    assert "待发主管" in prompt
    assert "已淘汰的" not in prompt


# -------------------------------------------------------------- 路由：槽位解析


async def test_plan_splits_comma_separated_name_string(services, positions) -> None:
    """模型偶尔把数组写成 "张明，李四"，不能整串当成一个人。"""
    llm = StubLLM({"intent": "query_candidates", "names": "张明，李四,王五"})

    plan = await _agent(services, llm=llm).plan(_inbound("这仨都看看"), positions=positions, roster=[])

    assert plan is not None
    assert plan.names == ["张明", "李四", "王五"]


async def test_plan_accepts_candidate_names_alias(services, positions) -> None:
    """有的模型用 candidate_names 而不是 names，两种都要认。"""
    llm = StubLLM({"intent": "query_candidate", "candidate_names": ["张明"]})

    plan = await _agent(services, llm=llm).plan(_inbound("张明呢"), positions=positions, roster=[])

    assert plan is not None
    assert plan.names == ["张明"]


async def test_plan_parses_count_from_text(services, positions) -> None:
    """「筛 5 份」里的 5 要抽出来；抽不出就当没说（0），不能当成筛 0 份。"""
    llm = StubLLM({"intent": "screen_top", "count": "5"})

    plan = await _agent(services, llm=llm).plan(_inbound("先给我筛 5 份"), positions=positions, roster=[])

    assert plan is not None
    assert plan.count == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(5, 5), ("5", 5), ("筛 5 份", 5), ("五份", 0), (True, 0), (-3, 0), (2.7, 2), (None, 0)],
)
def test_safe_int_never_returns_negative_or_bool(raw, expected) -> None:
    """份数是个「有就用、没有就别瞎猜」的槽位，负数/布尔值都不该漏进去。"""
    assert _safe_int(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [(3, 1.0), (-1, 0.0), ("abc", 0.0), (None, 0.0), (0.42, 0.42)])
async def test_plan_clamps_confidence(services, positions, raw, expected) -> None:
    """置信度必须落在 0~1，否则下游展示会出现「120% 把握」这种笑话。"""
    llm = StubLLM({"intent": "query_stats", "confidence": raw})

    plan = await _agent(services, llm=llm).plan(_inbound("今天多少份"), positions=positions, roster=[])

    assert plan is not None
    assert plan.confidence == pytest.approx(expected)


async def test_plan_keeps_raw_intent_for_troubleshooting(services, positions) -> None:
    """排查「为什么被判成 unknown」时，原始意图文本是唯一线索。"""
    llm = StubLLM({"intent": "看不清的手写体"})

    plan = await _agent(services, llm=llm).plan(_inbound("…"), positions=positions, roster=[])

    assert plan is not None
    assert plan.intent == AgentIntent.UNKNOWN
    assert plan.raw_intent == "看不清的手写体"


# ------------------------------------------------------------------ 作答：主路径


async def test_answer_sends_llm_text_and_records_event(
    services, supervisor, positions, backend_position
) -> None:
    """读类回答要发到主管飞书，并留一条 agent.answered 事件（事后能查「谁问过什么」）。"""
    await _candidate(services.session, backend_position, "张明", score=91.0)
    llm = StubLLM({"intent": "query_candidates"}, text="目前只有张明一位在流程中，91 分，还没安排面试。")
    agent = _agent(services, llm=llm)
    plan = await agent.plan(_inbound("现在都有谁"), positions=positions, roster=[])

    reply = await agent.answer(_inbound("现在都有谁"), supervisor, plan, positions=positions, active=[])

    assert reply.handled is True
    assert reply.action == "agent_query_candidates"
    assert reply.text.startswith("目前只有张明")
    assert reply.detail["sent"] is True
    outbound = await services.messages.list_outbound_feishu()
    assert len(outbound) == 1
    assert outbound[0].recipient == SUPERVISOR_OPEN_ID
    events = await services.events.list_recent(action="agent.answered")
    assert len(events) == 1
    assert events[0].object_id == supervisor.id
    assert events[0].detail["facts_rows"] >= 1


async def test_answer_truncates_overlong_text(services, supervisor, positions, backend_position) -> None:
    """模型偶尔长篇大论，飞书消息有长度上限，截断要在服务层做。"""
    await _candidate(services.session, backend_position, "张明")
    llm = StubLLM({"intent": "query_candidates"}, text="很长" * 2000)
    agent = _agent(services, llm=llm)
    plan = await agent.plan(_inbound("都有谁"), positions=positions, roster=[])

    reply = await agent.answer(_inbound("都有谁"), supervisor, plan, positions=positions, active=[])

    assert len(reply.text) == 1500


# ------------------------------------------------------------------ 作答：降级


async def test_answer_falls_back_to_rule_summary_when_llm_fails(
    services, supervisor, positions, backend_position
) -> None:
    """表达层挂了也不能装死：退回规则摘要，主管照样看得到数字。"""
    await _candidate(services.session, backend_position, "张明", score=88.0)
    llm = StubLLM({"intent": "query_stats"}, text_error=RuntimeError("模型 502"))
    agent = _agent(services, llm=llm)
    plan = await agent.plan(_inbound("今天收了多少份"), positions=positions, roster=[])

    reply = await agent.answer(_inbound("今天收了多少份"), supervisor, plan, positions=positions, active=[])

    assert "今天收到 1 份" in reply.text
    assert "张明" in reply.text
    assert reply.detail["sent"] is True


async def test_answer_chat_falls_back_to_ability_list(services, supervisor, positions) -> None:
    """闲聊意图没有数据可给，降级文案要告诉主管「我能帮你干什么」。"""
    llm = StubLLM({"intent": "chat"}, text="")  # 返回空字符串也要降级，不能回一条空消息
    agent = _agent(services, llm=llm)
    plan = await agent.plan(_inbound("你好"), positions=positions, roster=[])

    reply = await agent.answer(_inbound("你好"), supervisor, plan, positions=positions, active=[])

    assert "候选人" in reply.text
    assert "面试" in reply.text


async def test_answer_with_nothing_found_says_so(services, supervisor, positions) -> None:
    """一条数据都没有时，必须明说「没匹配到」，不能让模型自由发挥。"""
    llm = StubLLM({"intent": "query_candidate", "names": ["查无此人"]}, text="")
    agent = _agent(services, llm=llm)
    plan = await agent.plan(_inbound("查无此人怎么样"), positions=positions, roster=[])

    reply = await agent.answer(_inbound("查无此人怎么样"), supervisor, plan, positions=positions, active=[])

    assert "没有匹配到候选人" in reply.text


async def test_answer_with_no_data_at_all_says_try_again(services, supervisor, positions) -> None:
    """空库 + 模型给了空话时，最后兜底要给出可执行建议（换个说法/直接说名字）。"""
    llm = StubLLM({"intent": "query_stats"}, text="")
    agent = _agent(services, llm=llm)
    plan = await agent.plan(_inbound("最近怎么样"), positions=positions, roster=[])

    reply = await agent.answer(_inbound("最近怎么样"), supervisor, plan, positions=positions, active=[])

    assert "该主管名下还没有任何候选人记录" in reply.text


# ------------------------------------------------------------------ 检索：事实


async def test_facts_stats_separate_today_from_total(services, positions, backend_position) -> None:
    """统计要拆「今天」和「累计」：HR 早上问的一定是今天。"""
    yesterday = _now() - timedelta(days=1)
    await _candidate(services.session, backend_position, "张明", status=CandidateStatus.DISPATCHED)
    await _candidate(services.session, backend_position, "李四", status=CandidateStatus.SCREENED_REJECTED)
    await _candidate(
        services.session, backend_position, "王五",
        status=CandidateStatus.SCREENED_PASSED, created_at=yesterday,
    )
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_STATS), positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID
    )

    assert "今天收到 2 份" in facts.summary
    assert "通过 1 份" in facts.summary
    assert "淘汰 1 份" in facts.summary
    assert "累计 3 人" in facts.summary


async def test_facts_alive_list_excludes_rejected(services, positions, backend_position) -> None:
    """摘要里的「未淘汰名单」是主管判断「还剩几个人」的依据，淘汰的人不能混进来。"""
    await _candidate(services.session, backend_position, "张明")
    await _candidate(services.session, backend_position, "李四", status=CandidateStatus.SCREENED_REJECTED)
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_STATS), positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID
    )

    assert "未淘汰名单：张明" in facts.summary
    assert "李四" not in facts.summary


async def test_facts_query_candidates_reports_active_count(services, positions, backend_position) -> None:
    """问「有哪些人」时摘要换成「当前在流程中 N 人」，否则模型会把累计数当成在流程数。"""
    candidate = await _candidate(services.session, backend_position, "张明")
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_CANDIDATES), positions=positions, active=[candidate], sender_id=SUPERVISOR_OPEN_ID
    )

    assert facts.summary == "当前在流程中的候选人 1 人（按分数从高到低）"
    assert facts.rows[0]["姓名"] == "张明"


async def test_facts_empty_db_says_no_records(services, positions) -> None:
    """库里一条记录都没有时给一句人话，不要拼出「今天收到 0 份；累计 0 人：」。"""
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_STATS), positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID
    )

    assert facts.summary == "该主管名下还没有任何候选人记录"


async def test_facts_explain_score_flattens_score_detail(services, positions, backend_position) -> None:
    """解释分数要带上「硬条件过没过、加权命中了什么」，只给一个总分等于没解释。"""
    await _candidate(
        services.session, backend_position, "张明", score=79.0,
        score_detail={
            "reason": "硬条件全部通过，技能命中 4 项",
            "threshold": 60,
            "hard_failed": ["专业对口"],
            "hard_unknown": ["院校信息"],
            "hits": [{"label": "技术栈匹配度", "actual": "Python/Redis", "ratio": 0.8}],
            "semantic_score": 82,
            "semantic_note": "项目描述与岗位高度相关",
            "deep_analysis": {"strengths": ["高并发经验"], "risks": ["无大厂背景"]},
        },
    )
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.EXPLAIN_SCORE, ["张明"], "为什么 79 分"),
        positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID,
    )

    row = facts.rows[0]
    assert row["筛选结论"].startswith("硬条件全部通过")
    assert row["未通过的硬条件"] == ["专业对口"]
    assert row["字段缺失待复核"] == ["院校信息"]
    assert row["加权命中"][0]["项"] == "技术栈匹配度"
    assert row["加权命中"][0]["得分率"] == pytest.approx(0.8)
    assert row["语义分"] == 82
    assert row["亮点"] == ["高并发经验"]
    assert row["风险"] == ["无大厂背景"]


async def test_facts_resume_intent_returns_excerpt(services, positions, backend_position) -> None:
    """读简历要把正文带上（截 2500 字），否则模型只能凭摘要猜技术栈。"""
    resume = "张明，六年后端经验，熟悉 Python/FastAPI/PostgreSQL，" * 100
    await _candidate(services.session, backend_position, "张明", resume_text=resume, summary="后端老手")
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_RESUME, ["张明"], "会不会 Kafka"),
        positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID,
    )

    assert facts.resume_excerpt.startswith("张明，六年后端经验")
    assert len(facts.resume_excerpt) == 2500
    assert "后端老手" in facts.summary
    assert "筛选结论" not in facts.rows[0]  # 读简历不需要摊开打分细节


async def test_facts_without_match_reports_names_asked(services, positions, backend_position) -> None:
    """没匹配到人时要把「你问的是谁」带回去，方便主管自己发现打错字。"""
    await _candidate(services.session, backend_position, "张明")
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_CANDIDATE, ["李雷"]),
        positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID,
    )

    assert facts.summary.startswith("没有匹配到候选人（查找的是：李雷）")
    assert facts.rows[0]["姓名"] == "张明"  # 退回名单底稿，别让主管看到一片空白


# ---------------------------------------------------------- 检索：把人找出来


async def test_resolve_targets_finds_candidate_outside_active(services, positions, backend_position) -> None:
    """已发通知/已完成的人不在 active 里，但主管还是会问，必须能查到。"""
    await _candidate(services.session, backend_position, "张明", status=CandidateStatus.INTERVIEW_NOTIFIED)
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_CANDIDATE, ["张明"]),
        positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID,
    )

    assert facts.rows[0]["姓名"] == "张明"
    assert facts.rows[0]["状态"] == "已邮件通知"


async def test_resolve_targets_matches_surname(services, positions, backend_position) -> None:
    """主管常只说姓（「张那个怎么样」），精确匹配不到时要按包含再找一次。"""
    await _candidate(services.session, backend_position, "张明")
    agent = _agent(services, llm=DisabledLLM())

    facts = await agent._facts(
        _plan(AgentIntent.QUERY_CANDIDATE, ["张"]),
        positions=positions, active=[], sender_id=SUPERVISOR_OPEN_ID,
    )

    assert facts.rows[0]["姓名"] == "张明"


async def test_resolve_targets_without_names_uses_recent_active(services, backend_position) -> None:
    """没点名就答最近几个（最多 3 个），不能让模型自己挑人。"""
    for name in ("甲", "乙", "丙", "丁"):
        await _candidate(services.session, backend_position, name)
    agent = _agent(services, llm=DisabledLLM())
    active = await agent.candidates.list_by_position(backend_position.id)

    targets = await agent._resolve_targets(
        _plan(AgentIntent.QUERY_CANDIDATE), active=active, position_ids=[backend_position.id]
    )

    assert [c.name for c in targets] == [c.name for c in active[:3]]


# ------------------------------------------------------------------ 事实行格式


async def test_row_marks_interview_time_unarranged(services, backend_position) -> None:
    """「未安排」必须显式写出来，否则模型答不了「谁还没安排面试」。"""
    candidate = await _candidate(services.session, backend_position, "张明")

    row = _agent(services, llm=DisabledLLM())._row(candidate)

    assert row["面试时间"] == "未安排"


async def test_row_drops_empty_fields(services, backend_position) -> None:
    """0 分 / 空字符串这些占位值要剔掉，否则模型会把「分数 0」当真。"""
    candidate = await _candidate(services.session, backend_position, "张明", score=0.0)

    row = _agent(services, llm=DisabledLLM())._row(candidate)

    assert "分数" not in row
    assert row["姓名"] == "张明"


# -------------------------------------------------------------- 上下文：不串台


async def test_history_is_scoped_to_sender(services) -> None:
    """同一个群里三个人说话，上下文只能取自己那几句（2026-11-14 修的就是这个）。"""
    agent = _agent(services, llm=DisabledLLM())
    await services.messages.add(
        Message(channel=Channel.FEISHU, direction=Direction.INBOUND, sender="ou_a", body="我这轮要几个人")
    )
    await services.messages.add(
        Message(channel=Channel.FEISHU, direction=Direction.INBOUND, sender="ou_b", body="别人的问题")
    )
    await services.messages.add(
        Message(channel=Channel.FEISHU, direction=Direction.OUTBOUND, sender="bot", body="机器人回答")
    )

    lines = await agent._history(sender_id="ou_a", limit=6)

    assert "主管：我这轮要几个人" in lines
    assert "机器人：机器人回答" in lines
    assert all("别人的问题" not in line for line in lines)


async def test_history_without_sender_reads_latest_feishu(services) -> None:
    """内部调用（没有 sender）保持原行为：只取最近 limit 条飞书消息。"""
    agent = _agent(services, llm=DisabledLLM())
    for index in range(3):
        await services.messages.add(
            Message(channel=Channel.FEISHU, direction=Direction.INBOUND, sender=f"ou_{index}", body=f"第{index}句")
        )

    lines = await agent._history(sender_id="", limit=2)

    assert len(lines) == 2
    assert set(lines) == {"主管：第1句", "主管：第2句"}


async def test_history_ignores_other_channels(services) -> None:
    """邮件消息不能冒充飞书上下文，否则主管会在飞书里看到「邮件里说过的话」。"""
    agent = _agent(services, llm=DisabledLLM())
    await services.messages.add(
        Message(channel=Channel.EMAIL, direction=Direction.INBOUND, sender="a@b.com", body="邮件正文")
    )

    assert await agent._history(sender_id="", limit=6) == []


async def test_history_skips_empty_body(services) -> None:
    """留痕里的空 body（比如纯图片消息）不该占上下文位置。"""
    agent = _agent(services, llm=DisabledLLM())
    await services.messages.add(
        Message(channel=Channel.FEISHU, direction=Direction.INBOUND, sender="ou_a", body="  \n ")
    )

    assert await agent._history(sender_id="ou_a", limit=6) == []
