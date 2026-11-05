"""工程稳定性补齐测试（§4-A.1）：幂等防重 / 幂等补发 / 解析降级告警 / 异常隔离。

每条用例 docstring 注明保护的不变量；全部离线（mock 飞书 + mock 邮件）、确定性（固定时间）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from client.resume_parser import ResumeParser
from model.candidate import Candidate, CandidateStatus
from model.event import Event
from model.message import Channel, Direction, Message
from model.position import Position
from tests.conftest import read_outbox

NOW = datetime(2026, 11, 11, 14, 0, 0)  # 固定面试时间，保证确定性（不依赖"今天"）

SUCCESS_STATUS = ("sent", "mocked")


async def _make_candidate(session, *, code: str = "backend_senior", **overrides) -> tuple[Candidate, Position]:
    """直接在测试库中造一个「已分发、已定时间」的候选人（不依赖完整闭环）。"""
    position = (await session.execute(select(Position).where(Position.code == code))).scalars().one()
    fields = dict(
        name="测试候选人",
        email="candidate@example.com",
        phone="13900001111",
        education_level="本科",
        school="华北电力大学",
        major="计算机科学与技术",
        years_experience=6.0,
        position_id=position.id,
        position_code=position.code,
        status=CandidateStatus.DISPATCHED,
        interview_time=NOW,
        score=80.0,
    )
    fields.update(overrides)
    candidate = Candidate(**fields)
    session.add(candidate)
    await session.flush()
    return candidate, position


async def _successful_messages(notification, external_id: str) -> list[Message]:
    records = await notification.messages.list_recent(limit=200)
    return [m for m in records if m.external_id == external_id and m.status in SUCCESS_STATUS]


# --------------------------------------------------------------------- 幂等防重


async def test_interview_notice_same_message_id_sends_once(services, settings) -> None:
    """§4-A.1a 幂等防重：同一 Message-ID 重发两次，候选人只收到一封、
    messages 只有一条成功记录。保护：绝不给候选人重复发面试邮件。"""
    notification = services.notification
    candidate, position = await _make_candidate(notification.session)
    stable_id = "<stable-notice@example.com>"

    first = await notification.send_interview_notice(candidate, position, interview_time=NOW, message_id=stable_id)
    assert first.ok is True

    second = await notification.send_interview_notice(candidate, position, interview_time=NOW, message_id=stable_id)
    assert second.ok is True
    assert "跳过" in second.detail

    notices = [
        mail
        for mail in read_outbox(settings)
        if mail.to_addresses and mail.to_addresses[0] == "candidate@example.com"
    ]
    assert len(notices) == 1, "同一 Message-ID 第二次发送必须被幂等跳过"
    assert len(await _successful_messages(notification, stable_id)) == 1


async def test_first_send_without_explicit_id_still_sends_normally(services, settings) -> None:
    """§4-A.1a 回归保护：不传 message_id 时行为与旧版完全一致（新键、正常发送）。"""
    notification = services.notification
    candidate, position = await _make_candidate(notification.session)

    result = await notification.send_interview_notice(candidate, position, interview_time=NOW)
    assert result.ok is True
    assert result.message_id  # 由 mailbox.new_message_id() 生成
    assert len(await _successful_messages(notification, result.message_id)) == 1


# --------------------------------------------------------------------- 幂等补发


async def test_failed_notice_retried_without_duplicate(services, settings) -> None:
    """§4-A.1b 幂等补发：失败记录按原 Message-ID 补发成功；再次补发不产生重复邮件。
    保护：失败只补一次、绝不连发多封。"""
    notification = services.notification
    candidate, position = await _make_candidate(notification.session)

    failed = Message(
        channel=Channel.EMAIL,
        direction=Direction.OUTBOUND,
        sender=settings.hr_mailbox,
        recipient=candidate.email,
        subject="面试通知 | 测试",
        body="（上一轮发送失败的现场记录）",
        candidate_id=candidate.id,
        position_id=position.id,
        external_id="<failed-notice@example.com>",
        status="failed",
        error="smtp timeout",
        sent_at=datetime.utcnow(),
    )
    await notification.messages.add(failed)
    await notification.session.commit()

    results = await notification.retry_failed_interview_notices()
    assert len(results) == 1
    assert results[0].ok is True

    # 再触发一轮补发：幂等查重命中 → 跳过，不再发信
    again = await notification.retry_failed_interview_notices()
    assert len(again) == 1
    assert "跳过" in again[0].detail

    notices = [
        mail
        for mail in read_outbox(settings)
        if mail.to_addresses and mail.to_addresses[0] == "candidate@example.com"
    ]
    assert len(notices) == 1
    assert len(await _successful_messages(notification, "<failed-notice@example.com>")) == 1


async def test_reschedule_confirmation_failure_left_for_manual(services) -> None:
    """§4-A.1b 边界：改期确认邮件的失败不自动补发（I-06：时间必须来自主管确认），
    留给人工处理。"""
    notification = services.notification
    candidate, position = await _make_candidate(notification.session)

    await notification.messages.add(
        Message(
            channel=Channel.EMAIL,
            direction=Direction.OUTBOUND,
            sender=settings_hr(notification),
            recipient=candidate.email,
            subject="面试时间调整确认 | 测试",
            body="（改期确认发送失败）",
            candidate_id=candidate.id,
            position_id=position.id,
            external_id="<failed-reschedule@example.com>",
            status="failed",
            error="smtp timeout",
            sent_at=datetime.utcnow(),
        )
    )
    await notification.session.commit()

    results = await notification.retry_failed_interview_notices()
    assert results == [], "改期确认失败必须留给人工，不自动补发"


def settings_hr(notification) -> str:
    return notification.settings.hr_mailbox


async def test_feishu_failed_message_retried_once(services) -> None:
    """§4-A.1b 飞书补发：失败出站消息按「收件人+正文」查重后补发一次；
    再次补发不再重发（应用层幂等）。"""
    notification = services.notification

    await notification.messages.add(
        Message(
            channel=Channel.FEISHU,
            direction=Direction.OUTBOUND,
            sender="ai-hr-robot",
            recipient="ou_mock_zhangwei",
            body="（稳定性测试）这条飞书消息发送失败了",
            external_id="",
            status="failed",
            error="api down",
            sent_at=datetime.utcnow(),
        )
    )
    await notification.session.commit()

    assert await notification.retry_failed_feishu_messages() == 1
    assert await notification.retry_failed_feishu_messages() == 0, "同内容已发出的不得重复发送"

    records = await notification.messages.list_recent(limit=200)
    resent = [
        m
        for m in records
        if m.channel == Channel.FEISHU
        and m.recipient == "ou_mock_zhangwei"
        and m.body == "（稳定性测试）这条飞书消息发送失败了"
        and m.status in SUCCESS_STATUS
    ]
    assert len(resent) == 1


# --------------------------------------------------------------------- 异常隔离


async def test_run_daily_cycle_continues_when_step_fails(services, monkeypatch) -> None:
    """§4-A.1e 异常捕获：run_daily_cycle 中任一步抛错，其余步骤仍执行、闭环正常收尾。
    保护：I-10（单点失败不阻断全局）。"""
    orchestrator = services.orchestrator

    async def boom() -> dict[str, int]:
        raise RuntimeError("配置读取炸了")

    monkeypatch.setattr(orchestrator, "sync_config", boom)
    response = await orchestrator.run_daily_cycle()
    assert any("配置同步失败" in e for e in response.errors)

    completed = (
        await orchestrator.session.execute(select(Event).where(Event.action == "cycle.completed"))
    ).scalars().all()
    assert completed, "出错后本轮仍必须走到 cycle.completed 收尾"


async def test_single_position_failure_does_not_block_others(services, monkeypatch) -> None:
    """I-10：dispatch_cycle 中单个职位抛错，其他职位仍正常分发。
    保护：一个部门配置错误不能拖垮整轮任务。"""
    notification = services.notification
    session = notification.session
    for code in ("backend_senior", "product_manager"):
        await _make_candidate(
            session,
            code=code,
            status=CandidateStatus.SCREENED_PASSED,
            cycle_id=2026110301,
        )
    await session.commit()

    original_send_text = notification.feishu.send_text

    async def flaky_send_text(open_id: str, text: str):
        if open_id == "ou_mock_lina":  # 只让产品部主管的发送抛错
            raise RuntimeError("飞书挂了")
        return await original_send_text(open_id, text)

    monkeypatch.setattr(notification.feishu, "send_text", flaky_send_text)
    results = await notification.dispatch_cycle(2026110301)

    by_code = {r.position_code: r for r in results}
    assert set(by_code) == {"backend_senior", "product_manager"}
    assert by_code["backend_senior"].ok is True, "技术部分发不受产品部失败影响"
    assert by_code["product_manager"].ok is False
    assert "飞书挂了" in by_code["product_manager"].error


async def test_cycle_has_no_failed_notice_initially(services) -> None:
    """§4-A.1b 回归保护：全新库上补发入口是 no-op——首轮 demo 输出不受影响。"""
    notification = services.notification
    assert await notification.retry_failed_interview_notices() == []
    assert await notification.retry_failed_feishu_messages() == 0


@pytest.mark.parametrize(
    ("external_id", "status", "expected"),
    [
        ("<m1@example.com>", "mocked", True),
        ("<m1@example.com>", "sent", True),
        ("<m1@example.com>", "failed", False),
        ("", "mocked", False),
    ],
)
async def test_find_delivered_semantics(services, external_id, status, expected) -> None:
    """§4-A.1a 幂等键语义：只有 sent/mocked 拦截重发，failed 不拦截、空键不查。"""
    notification = services.notification
    if external_id:
        await notification.messages.add(
            Message(
                channel=Channel.EMAIL,
                direction=Direction.OUTBOUND,
                sender="hr@xingye-test.com",
                recipient="x@example.com",
                body="幂等语义测试",
                external_id=external_id,
                status=status,
            )
        )
        await notification.session.flush()
    delivered = await notification.messages.find_delivered("<m1@example.com>")
    assert (delivered is not None) is expected
