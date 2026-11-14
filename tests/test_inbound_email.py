"""入站邮件分流（``service/inbound_email.py``）测试。

这个模块是「所有进来的邮件」的唯一入口，它只做一件事：**判这封邮件属于哪一类**，
然后交给对应的服务。判断错了不会报错，只会安静地做错事：

- 把新简历当成某位候选人的回信 → 附件不入库，简历凭空消失；
- 把候选人放弃当成闲聊 → 主管一直等一个不会来的人；
- 同一封邮件处理两遍 → 主管收到两条一模一样的通知。

文件按「闸门 → 四个分流 → 轮询统计 → 判据」组织。邮件对象直接构造，不走 IMAP，
飞书/模板用 mock；只有数据库是真的 PostgreSQL。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from client.mailbox import InboundEmail, MailAttachment
from model.candidate import Candidate, CandidateStatus
from model.event import Event
from model.message import Channel, Direction, Message
from model.position import Position
from service.inbound_email import _is_resume_email
from tests.conftest import resume_text

SUPERVISOR_EMAIL = "zhangwei@xingye-test.com"


# --------------------------------------------------------------------- 构造工具


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _email(
    *,
    sender: str = "candidate@example.com",
    subject: str = "应聘高级后端工程师",
    body: str = "您好，附件是我的简历，请查收。",
    message_id: str | None = None,
    attachments: list[MailAttachment] | None = None,
    in_reply_to: str = "",
    to: tuple[str, ...] = ("hr@xingye-test.com",),
) -> InboundEmail:
    return InboundEmail(
        message_id=message_id or f"<{uuid.uuid4().hex}@example.com>",
        subject=subject,
        from_address=sender,
        from_name="",
        to_addresses=list(to),
        in_reply_to=in_reply_to,
        body_text=body,
        received_at=_now(),
        attachments=attachments or [],
    )


def _attachment(filename: str = "张明-高级后端工程师.txt") -> MailAttachment:
    return MailAttachment(
        filename=filename,
        content_type="text/plain",
        data=resume_text().encode("utf-8"),
    )


async def _candidate(
    services,
    position: Position | None,
    *,
    name: str = "张明",
    email: str = "zhangming@example.com",
    phone: str = "13800001111",
    status: str = CandidateStatus.DISPATCHED,
) -> Candidate:
    candidate = Candidate(
        name=name,
        email=email,
        phone=phone,
        education_level="本科",
        school="华北电力大学",
        major="计算机科学与技术",
        years_experience=6.0,
        position_id=position.id if position else None,
        position_code=position.code if position else "",
        status=status,
        score=88.0,
    )
    services.session.add(candidate)
    await services.session.flush()
    return candidate


@pytest.fixture
async def backend_position(services) -> Position:
    row = await services.session.execute(select(Position).where(Position.code == "backend_senior"))
    return row.scalars().one()


async def _feishu_out(services) -> list[Message]:
    return await services.messages.list_outbound_feishu()


# ------------------------------------------------------------------ 闸门：幂等


async def test_duplicate_message_id_is_skipped(services) -> None:
    """同一封邮件（同一 Message-ID）只能处理一次：IMAP 水位丢失会重拉历史邮件。"""
    message_id = "<dup-001@example.com>"
    await services.messages.add(
        Message(
            channel=Channel.EMAIL,
            direction=Direction.INBOUND,
            sender="candidate@example.com",
            body="已经处理过的正文",
            external_id=message_id,
            status="received",
        )
    )

    outcome = await services.inbound.handle_email(_email(message_id=message_id))

    assert outcome.kind == "duplicate"
    assert outcome.handled is False
    assert "重复" in outcome.detail["reason"]


async def test_different_message_ids_are_all_processed(services) -> None:
    """判重只认 Message-ID，不能因为同一个人连发两封就吞掉第二封。"""
    first = await services.inbound.handle_email(
        _email(sender="nobody@example.com", body="你好，请问公司在哪？")
    )
    second = await services.inbound.handle_email(
        _email(sender="nobody@example.com", body="补充一句：我下周可以到岗。")
    )

    assert first.kind == "unmatched"
    assert second.kind == "unmatched"


# -------------------------------------------------------- 分流 0：带附件优先入库


async def test_resume_email_goes_to_ingestion(services) -> None:
    """正常投递：附件解析入库，返回新建了谁，主管那边不需要人肉确认。"""
    outcome = await services.inbound.handle_email(
        _email(sender="zhangming@example.com", attachments=[_attachment()])
    )

    assert outcome.kind == "resume"
    assert outcome.handled is True
    assert outcome.detail["created"] == 1
    assert "张明" in outcome.detail["candidates"]
    candidate = (
        await services.session.execute(select(Candidate).where(Candidate.name == "张明"))
    ).scalars().first()
    assert candidate is not None


async def test_platform_sender_with_resume_attachment_is_still_ingested(services, backend_position) -> None:
    """2026-11-12 的核心修复：平台共用发件人（cv@notice.zhipin.com）发来的新简历必须入库。

    旧逻辑按发件人认人，第一封之后每封新简历都被当成「同一位候选人的回信」，
    附件直接丢掉——实测 20 封平台邮件里 9 封被误判成 candidate.reply.confirm。
    """
    # 造一个「邮箱被平台地址污染」的候选人，模拟历史数据。
    # 手机号故意与简历里的不同：去重按「邮箱 + 手机号」，这里要验证的是分流而不是去重。
    polluted = await _candidate(
        services, backend_position, name="老候选人",
        email="cv@notice.zhipin.com", phone="13900009999",
    )

    outcome = await services.inbound.handle_email(
        _email(
            sender="cv@notice.zhipin.com",
            subject="新简历投递：李雷",
            body="您好，以下候选人投递了您的职位，简历见附件。",
            attachments=[_attachment("李雷-后端.txt")],
        )
    )

    assert outcome.kind == "resume"
    assert outcome.detail["created"] == 1
    # 老候选人不能被当成回信处理（状态没变）
    assert polluted.status == CandidateStatus.DISPATCHED


async def test_supervisor_email_with_resume_attachment_also_goes_to_ingestion(services) -> None:
    """主管转发的简历也是简历：带附件就优先入库，而不是当成主管反馈。"""
    outcome = await services.inbound.handle_email(
        _email(sender=SUPERVISOR_EMAIL, subject="转发一份简历", attachments=[_attachment("王五.txt")])
    )

    assert outcome.kind == "resume"
    assert outcome.detail["created"] == 1


# ------------------------------------------------------ 分流 1：候选人回信


async def test_candidate_decline_marks_status_and_notifies_supervisor(services, backend_position) -> None:
    """候选人放弃：状态变「已放弃」，主管立刻收到一条带处置建议的飞书。"""
    candidate = await _candidate(services, backend_position)

    outcome = await services.inbound.handle_email(
        _email(sender=candidate.email, body="非常抱歉，我这边已经找到工作了，放弃这次面试机会。")
    )

    assert outcome.kind == "candidate_decline"
    assert outcome.handled is True
    assert candidate.status == CandidateStatus.DECLINED
    outbound = await _feishu_out(services)
    assert len(outbound) == 1
    assert "张明" in outbound[0].body
    events = await services.events.list_recent(action="candidate.reply.decline")
    assert len(events) == 1
    assert events[0].from_state == CandidateStatus.DISPATCHED
    assert events[0].to_state == CandidateStatus.DECLINED


async def test_candidate_confirm_marks_completed(services, backend_position) -> None:
    """候选人确认参加：状态走「已完成」，事件里留 Message-ID 便于回溯是哪封信。"""
    candidate = await _candidate(services, backend_position)
    email = _email(sender=candidate.email, body="收到，我会准时参加，谢谢！")

    outcome = await services.inbound.handle_email(email)

    assert outcome.kind == "candidate_confirm"
    assert candidate.status == CandidateStatus.COMPLETED
    events = await services.events.list_recent(action="candidate.reply.confirm")
    assert events[0].detail["message_id"] == email.message_id


async def test_candidate_reschedule_keeps_proposed_time(services, backend_position) -> None:
    """改期：状态转「改期中」，候选人说的新时间必须解析出来传给主管，否则主管得自己猜。"""
    candidate = await _candidate(services, backend_position)

    outcome = await services.inbound.handle_email(
        _email(sender=candidate.email, body="不好意思，那天有冲突，能否改到明天下午 3 点？")
    )

    assert outcome.kind == "candidate_reschedule"
    assert candidate.status == CandidateStatus.RESCHEDULE_PENDING
    assert outcome.detail["proposed_time"] is not None
    outbound = await _feishu_out(services)
    assert "改期" in outbound[0].body


async def test_candidate_reply_without_position_skips_notification(services) -> None:
    """没归位到岗位的候选人（附邮箱但岗位为空）也要处理，只是没人可通知。"""
    candidate = await _candidate(services, None, name="散件候选人", email="scattered@example.com")

    outcome = await services.inbound.handle_email(
        _email(sender=candidate.email, body="我放弃这次面试了。")
    )

    assert outcome.kind == "candidate_decline"
    assert candidate.status == CandidateStatus.DECLINED
    assert await _feishu_out(services) == []


# ------------------------------------------------------ 分流 2：主管反馈


async def test_supervisor_email_goes_to_feedback_service(services) -> None:
    """主管回信走和飞书完全相同的 FeedbackService 路径，不另写一套排期逻辑。"""
    outcome = await services.inbound.handle_email(
        _email(sender=SUPERVISOR_EMAIL, subject="回复：简历包", body="这两个人都可以，安排一下面试。")
    )

    assert outcome.kind == "supervisor_feedback"
    assert "action" in outcome.detail


async def test_supervisor_email_creates_thread_for_followup(services) -> None:
    """主管回信没有命中线程时要补登记一条，否则他回第二封就认不出来了。"""
    email = _email(sender=SUPERVISOR_EMAIL, subject="我的看法", body="先都安排面试吧。")

    await services.inbound.handle_email(email)

    from core.threading import normalize_message_id
    from model.email_thread import EmailThread

    threads = (await services.session.execute(select(EmailThread))).scalars().all()
    # 2026-11-14 起线程里的 Message-ID 统一去尖括号存储（收信解析就是这个格式），
    # 所以这里也按归一化后比较，否则「带不带 <>」会让断言假失败。
    expected = normalize_message_id(email.message_id)
    assert any(normalize_message_id(t.root_message_id) == expected for t in threads)


# ------------------------------------------------------ 分流 4：无法归类


async def test_unmatched_email_is_recorded_and_ignored(services) -> None:
    """认不出来的邮件也要留痕 + 留事件：以后排查「那封邮件去哪了」只能靠这两张表。"""
    email = _email(sender="stranger@example.com", subject="商务合作", body="你好，请问公司在哪？")

    outcome = await services.inbound.handle_email(email)

    assert outcome.kind == "unmatched"
    assert outcome.handled is False
    stored = (
        await services.session.execute(
            select(Message).where(Message.external_id == email.message_id)
        )
    ).scalars().first()
    assert stored is not None
    assert stored.direction == Direction.INBOUND
    events = await services.events.list_recent(action="inbound.unmatched")
    assert len(events) == 1
    assert events[0].detail["from"] == "stranger@example.com"


async def test_platform_sender_without_attachment_is_not_treated_as_candidate(services, backend_position) -> None:
    """平台通知地址即使被写进了候选人的邮箱，也不能当成那个人在回信。"""
    polluted = await _candidate(
        services, backend_position, name="老候选人",
        email="cv@notice.zhipin.com", phone="13900009999",
    )
    body = "您好，" + "本次投递状态有更新，请登录后台查看。" * 10 + "简历进度：待处理。"

    outcome = await services.inbound.handle_email(_email(sender="cv@notice.zhipin.com", body=body))

    assert outcome.kind == "unmatched"
    assert polluted.status == CandidateStatus.DISPATCHED


# ---------------------------------------------------------------- 轮询统计


async def test_poll_once_splits_processed_ignored_and_failed(services, monkeypatch) -> None:
    """一封失败不能拖垮整轮：成功的算 processed，没归类的算 ignored，出错的单列 failed。"""
    resume_mail = _email(sender="zhangming@example.com", attachments=[_attachment()])
    chatter = _email(sender="stranger@example.com", body="你好，请问公司在哪？")
    boom = _email(sender="boom@example.com", subject="boom", body="炸掉这封")

    async def fake_poll():
        return [resume_mail, chatter, boom]

    original_match = services.inbound.threads.match

    async def flaky_match(email, *args, **kwargs):
        if email.subject == "boom":
            raise RuntimeError("模拟线程查询失败")
        return await original_match(email, *args, **kwargs)

    monkeypatch.setattr(services.inbound.mailbox, "poll_inbox", fake_poll)
    monkeypatch.setattr(services.inbound.threads, "match", flaky_match)

    response = await services.inbound.poll_once(cycle_id=None)

    assert (response.processed, response.ignored, response.failed) == (1, 1, 1)
    assert len(response.details) == 2  # 失败的那封不进 details
    assert "模拟线程查询失败" in response.errors[0]
    assert {d["kind"] for d in response.details} == {"resume", "unmatched"}


async def test_poll_once_commits_results(services, monkeypatch) -> None:
    """poll_once 结尾必须提交，否则进程被下一轮覆盖时简历就丢了。"""
    resume_mail = _email(sender="lisi@example.com", attachments=[_attachment("李四.txt")])

    async def fake_poll():
        return [resume_mail]

    monkeypatch.setattr(services.inbound.mailbox, "poll_inbox", fake_poll)

    response = await services.inbound.poll_once()

    assert response.processed == 1
    # 换一个全新会话读：能读到就说明确实提交了
    async with services.container.session_factory() as fresh:
        names = (await fresh.execute(select(Candidate.name))).scalars().all()
    assert "张明" in names


async def test_thread_attach_failure_does_not_break_handling(services, backend_position, monkeypatch) -> None:
    """线程补登记只是「下次能认出来」的优化，失败不能影响这次回复的处理。"""
    candidate = await _candidate(services, backend_position)

    async def boom(*args, **kwargs):
        raise RuntimeError("线程表写失败")

    monkeypatch.setattr(services.inbound.threads, "attach_thread", boom)

    outcome = await services.inbound.handle_email(
        _email(sender=candidate.email, body="我放弃这次面试了。")
    )

    assert outcome.kind == "candidate_decline"
    assert candidate.status == CandidateStatus.DECLINED


# ---------------------------------------------------------------- 判据本身


def test_is_resume_email_by_attachment_suffix() -> None:
    """带简历类后缀的附件直接算简历，不看正文长短。"""
    email = _email(body="见附件", attachments=[_attachment("简历.docx")])

    assert _is_resume_email(email) is True


def test_is_resume_email_ignores_unknown_attachment() -> None:
    """压缩包/图片等非简历后缀不触发入库，避免把签名图片当成简历解析。"""
    email = _email(
        body="见附件",
        attachments=[MailAttachment(filename="logo.png", content_type="image/png", data=b"fake")],
    )

    assert _is_resume_email(email) is False


def test_short_body_with_resume_words_is_not_resume() -> None:
    """短句里出现「简历」多半是「我的简历收到了吗」，不能因此建一个空候选人。"""
    assert _is_resume_email(_email(body="我的简历收到了吗？")) is False


def test_long_body_with_multiple_resume_hints_is_resume() -> None:
    """没有附件但正文像简历（教育背景 + 工作经历等 ≥2 个特征、≥120 字）时也要收下。"""
    body = (
        "教育背景：华北电力大学 计算机科学与技术 本科\n"
        "工作经历：某电商公司后端开发六年，负责订单与支付链路。\n"
        "项目经验：主导订单服务重构，QPS 提升 3 倍。\n"
        "技能：Python、FastAPI、PostgreSQL、Redis、Docker。\n"
    ) * 2

    assert len(body) >= 120
    assert _is_resume_email(_email(body=body)) is True


def test_long_body_with_single_hint_is_not_resume() -> None:
    """只有一个特征词时宁可不收：误收会污染去重口径，漏收还能靠人工补。"""
    body = "这是一封关于工作安排的通知。" * 10 + "简历请见附件。"

    assert len(body) >= 120
    assert _is_resume_email(_email(body=body)) is False


async def test_poll_once_rolls_back_after_db_error(services, monkeypatch) -> None:
    """单封邮件把事务搞坏后，必须回滚，否则后面的邮件和结尾 commit 全部连带失败。

    2026-11-14 真实事故：手动收信与定时轮询同时跑，撞上 messages 的幂等唯一索引；
    11 封邮件其实都入库了，接口却回 500——因为异常被捕获后没有回滚，
    会话一直处于「已中止的事务」状态，结尾的 commit 再抛一次。
    """
    from sqlalchemy import func, select, text

    from model.candidate import Candidate

    boom = _email(sender="boom@example.com", subject="boom", body="这封会让事务中止")
    resume_mail = _email(sender="lisi@example.com", attachments=[_attachment("李四.txt")])

    async def fake_poll():
        return [boom, resume_mail]

    original = services.inbound.handle_email

    async def flaky(email, **kwargs):
        if email.subject == "boom":
            await services.session.execute(text("select 1/0"))  # 模拟数据库层异常
        return await original(email, **kwargs)

    monkeypatch.setattr(services.inbound.mailbox, "poll_inbox", fake_poll)
    monkeypatch.setattr(services.inbound, "handle_email", flaky)

    response = await services.inbound.poll_once()

    assert response.failed == 1
    assert response.processed == 1, "第一封失败不应该影响第二封"
    # 会话还能用：能查到第二封入库的候选人（没回滚的话这里会抛 PendingRollbackError）
    count = (await services.session.execute(select(func.count()).select_from(Candidate))).scalar_one()
    assert count == 1
