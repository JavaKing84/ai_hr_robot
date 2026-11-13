"""面试通知「人工确认闸门」测试（测试期强制人工放行）。

保护的不变量：
1. 闸门开启时，主管给出名单只会生成草稿，绝不调用 SMTP；
2. 只有 HR 显式确认后，草稿才真正投递，候选人状态才变成「已邮件通知」；
3. 重复确认是幂等的，不会给候选人发第二封。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from model.candidate import Candidate, CandidateStatus
from model.message import Message
from model.position import Position
from schema.message import InboundMessage

NOW = datetime(2026, 11, 20, 6, 0, 0)
SUCCESS_STATUS = ("sent", "mocked")


async def _candidate_with_position(session) -> tuple[Candidate, Position]:
    """造一个已分发、带邮箱的候选人。"""
    position = (await session.execute(select(Position).where(Position.code == "backend_senior"))).scalars().one()
    candidate = Candidate(
        name="闸门测试候选人",
        email="gate@example.com",
        phone="13900002222",
        education_level="本科",
        school="华北电力大学",
        major="计算机科学与技术",
        years_experience=6.0,
        position_id=position.id,
        position_code=position.code,
        status=CandidateStatus.DISPATCHED,
        score=88.0,
    )
    session.add(candidate)
    await session.flush()
    return candidate, position


async def _delivered(services) -> list[Message]:
    """已经真正投递（含 mock 投递）的出站邮件。"""
    records = await services.notification.messages.list_recent(limit=200)
    # 只算真正投递出去的候选人邮件；飞书出站与 pending_approval 草稿不算
    return [
        m
        for m in records
        if m.status in SUCCESS_STATUS and m.channel == "email" and m.direction == "outbound"
    ]


async def test_gate_parks_then_sends_after_confirm(services, settings) -> None:
    """闸门开启：先挂起不发送，确认后才发出，且重复确认不重发。"""
    settings.interview_notice_require_confirm = True
    candidate, position = await _candidate_with_position(services.session)

    result = await services.notification.send_interview_notice(
        candidate, position, interview_time=NOW, location="北京市海淀区"
    )

    assert result.ok is True
    assert result.pending is True, "闸门开启时必须只挂起"
    assert candidate.status == CandidateStatus.INTERVIEW_PENDING_CONFIRM
    assert not await _delivered(services), "挂起阶段不能有任何投递记录"

    pending = await services.notification.pending_interview_notices()
    assert len(pending) == 1
    assert pending[0].status == "pending_approval"
    assert pending[0].recipient == "gate@example.com"

    results = await services.notification.confirm_pending_interview_notices([pending[0].id])
    assert len(results) == 1
    assert results[0].ok is True and results[0].pending is False
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED

    delivered = await _delivered(services)
    assert len(delivered) == 1, "确认后应恰好投递一封"
    assert await services.notification.pending_interview_notices() == []

    again = await services.notification.confirm_pending_interview_notices(None)
    assert again == [], "重复确认必须幂等"
    assert len(await _delivered(services)) == 1


async def test_gate_off_sends_directly(services, settings) -> None:
    """闸门关闭：保持原有行为，主管给出名单后直接投递。"""
    settings.interview_notice_require_confirm = False
    candidate, position = await _candidate_with_position(services.session)

    result = await services.notification.send_interview_notice(
        candidate, position, interview_time=NOW, location="北京市海淀区"
    )

    assert result.ok is True
    assert result.pending is False
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED
    assert len(await _delivered(services)) == 1
    assert await services.notification.pending_interview_notices() == []


def _feishu_reply(settings, text: str) -> InboundMessage:
    """构造一条来自「该岗位主管」的飞书回复。"""
    open_id = ""
    for department in settings.load_company_config().departments:
        if any(position.code == "backend_senior" for position in department.positions):
            open_id = department.supervisor.feishu_open_id
            break
    assert open_id, "配置里找不到 backend_senior 所属部门的主管 open_id"
    return InboundMessage(
        channel="feishu",
        sender_id=open_id,
        sender_name="主管",
        text=text,
        message_id=f"om_gate_{uuid.uuid4().hex[:8]}",
        received_at=datetime.now(),
    )


async def _parked_candidate(services, settings):
    """挂起一份面试通知，并拿到那次「飞书请示」记录。"""
    settings.interview_notice_require_confirm = True
    candidate, position = await _candidate_with_position(services.session)
    result = await services.notification.send_interview_notice(
        candidate, position, interview_time=NOW, location="北京市海淀区"
    )
    assert result.pending is True
    pending = await services.notification.pending_interview_notices()
    assert len(pending) == 1
    return candidate, pending


async def test_feishu_reply_confirms_pending_notice(services, settings) -> None:
    """HR 在飞书回复「确认」后，挂起的面试通知才真正投递。"""
    candidate, _ = await _parked_candidate(services, settings)
    assert not await _delivered(services), "确认前不能投递"

    outcome = await services.feedback.handle(_feishu_reply(settings, "确认"))

    assert outcome.handled is True
    assert outcome.action == "interview_notice_confirmed"
    assert outcome.detail["sent"] == 1
    await services.session.refresh(candidate)
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED
    assert len(await _delivered(services)) == 1
    assert await services.notification.pending_interview_notices() == []


async def test_feishu_reply_cancel_discards_pending_notice(services, settings) -> None:
    """HR 在飞书回复「取消」后，草稿作废且不投递，候选人退回已分发。"""
    candidate, _ = await _parked_candidate(services, settings)

    outcome = await services.feedback.handle(_feishu_reply(settings, "取消"))

    assert outcome.handled is True
    assert outcome.action == "interview_notice_cancelled"
    assert outcome.detail["cancelled"] == 1
    await services.session.refresh(candidate)
    assert candidate.status == CandidateStatus.DISPATCHED
    assert not await _delivered(services)
    assert await services.notification.pending_interview_notices() == []


async def test_time_reply_is_not_treated_as_approval(services, settings) -> None:
    """带时间表达的回复是「安排面试」，不能被当成对通知的确认。"""
    await _parked_candidate(services, settings)

    outcome = await services.feedback.handle(_feishu_reply(settings, "下周三下午 15:00 再安排一次"))

    assert outcome.action != "interview_notice_confirmed"
    # 仍然挂着，等真正的确认
    assert await services.notification.pending_interview_notices()

