"""主管在邮箱里回复这条链路（问题2 方案A）的端到端测试。

链路本身早就写好了，但一直没人跑通过：``email_threads`` 表 0 行、零测试。
写这批用例时实测到两处断点，本文件就是钉住它们的：

1. **认人丢信**：``supervisors.feishu_open_id`` 是唯一索引，三位主管共用同一个飞书号
   （演示环境里都是 HR 本人）时，同步后表里只剩一位。其他主管从自己邮箱回复，
   线程明明命中了，进到 ``feedback.handle`` 又被判成「不是已登记的主管」→ 回信静默消失。
   现在改成：查不到人时按 ``positions.supervisor_email`` 兜底。
2. **请示问错地方**：主管在邮箱里说「请通知张三」，机器人却跑到飞书问「确认发送吗」，
   他看不到，面试通知永远挂在待确认。现在改成：指令从哪个渠道来，请示就回哪个渠道。

另外钉住「带附件的回信」：主管用转发、或在回复里又拖进一份简历时，这封回信同样带
简历附件。它在简历包线程上，必须仍按主管指令处理——否则他的指令被当成一次投递，
压缩包里的简历还会重复入库一遍。

所有用例都不走 IMAP/SMTP：邮件构造后直接调 ``handle_email``，投递写本地 outbox，
只有数据库是真的 PostgreSQL。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from client.mailbox import InboundEmail, MailAttachment
from model.candidate import Candidate, CandidateStatus
from model.email_thread import EmailThread, ThreadKind
from model.event import Event
from model.message import Channel, Direction, Message
from model.position import Position
from model.supervisor import Supervisor
from schema.message import InboundMessage
from tests.conftest import read_outbox, resume_text

SUPERVISOR_EMAIL = "zhangwei@xingye-test.com"   # tests/fixtures/positions.test.yaml 里技术部主管
SUPERVISOR_OPEN_ID = "ou_mock_zhangwei"
INSTRUCTION = "请通知张明，下周三下午 14:00 来公司面试"


# --------------------------------------------------------------------- 构造工具


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _reply(
    *,
    sender: str = SUPERVISOR_EMAIL,
    body: str = INSTRUCTION,
    in_reply_to: str = "",
    to: tuple[str, ...] = ("hr@xingye-test.com",),
    subject: str = "Re: 【简历筛选】技术部·高级后端工程师 本轮通过 1 份",
    attachments: list[MailAttachment] | None = None,
    message_id: str | None = None,
) -> InboundEmail:
    return InboundEmail(
        message_id=message_id or f"<{uuid.uuid4().hex}@example.com>",
        subject=subject,
        from_address=sender,
        from_name="张伟",
        to_addresses=list(to),
        in_reply_to=in_reply_to,
        body_text=body,
        received_at=_now(),
        attachments=attachments or [],
    )


def _resume_attachment(filename: str = "李四-高级后端工程师.txt") -> MailAttachment:
    return MailAttachment(
        filename=filename,
        content_type="text/plain",
        data=resume_text(name="李四", email="lisi@example.com", phone="13800002222").encode("utf-8"),
    )


async def _position(services, code: str = "backend_senior") -> Position:
    row = await services.session.execute(select(Position).where(Position.code == code))
    return row.scalars().one()


async def _candidate(
    services,
    position: Position,
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
        position_id=position.id,
        position_code=position.code,
        status=status,
        score=88.0,
    )
    services.session.add(candidate)
    await services.session.flush()
    return candidate


async def _send_package(services, settings, position: Position, candidates: list[Candidate]) -> str:
    """真实走一遍「发简历包给主管」，拿回它带出去的 Message-ID。"""
    package = settings.var_dir / "packages" / "demo.zip"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"PK\x03\x04 demo package")
    result = await services.notification._send_supervisor_email(
        position, candidates, package, "简历包.zip", received_count=len(candidates) + 4
    )
    await services.session.commit()
    assert result.ok is True, result.detail
    return result.message_id


async def _thread_by_root(services, message_id: str) -> EmailThread | None:
    # 走仓库方法而不是直接查：它负责把 ``<...>`` 归一化，与存储格式对齐。
    return await services.threads.threads.find_by_root_message_id(message_id)


async def _drop_supervisor_row(services, email: str = SUPERVISOR_EMAIL) -> None:
    """模拟生产环境：supervisors 表因唯一索引只剩一位主管，其他主管查不到。"""
    row = await services.session.execute(select(Supervisor).where(Supervisor.email == email))
    supervisor = row.scalars().first()
    assert supervisor is not None, "测试前置条件：夹具同步后应有这位主管"
    await services.session.delete(supervisor)
    await services.session.flush()


async def _candidate_count(services) -> int:
    return int(
        (await services.session.execute(select(func.count()).select_from(Candidate))).scalar_one()
    )


async def _outbox_subjects(settings) -> list[str]:
    return [mail.subject for mail in read_outbox(settings)]


# ------------------------------------------------------- 发出的邮件本身要有引导


async def test_package_email_tells_supervisor_not_to_attach(services, settings) -> None:
    """正文必须写明「直接回复、不用加附件」和「请勿转发」。

    主管收到带 zip 的邮件，点回复后发现附件区空了，很容易以为要自己再附一份；
    一附就变成「带附件的回信」，历史上会被判成新简历投递。
    """
    position = await _position(services)
    candidate = await _candidate(services, position)
    await _send_package(services, settings, position, [candidate])

    mails = read_outbox(settings)
    assert len(mails) == 1
    body = mails[0].body_text
    assert "不需要另外添加附件" in body
    assert "请勿转发" in body
    assert candidate.name in body, "正文要列出通过筛选的人名，主管才知道通知谁"


# ----------------------------------------------------- 主链路：回复 → 排出面试


async def test_reply_on_package_thread_schedules_interview(services, settings) -> None:
    """主管在原邮件上回复 → 认得出是他 → 排出面试 → 候选人收到通知。"""
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])

    outcome = await services.inbound.handle_email(_reply(in_reply_to=message_id))

    assert outcome.kind == "supervisor_feedback"
    assert outcome.handled is True, outcome.detail
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED
    assert candidate.interview_time is not None
    subjects = await _outbox_subjects(settings)
    assert any("面试通知" in s for s in subjects), f"候选人应收到面试邀请，实际 outbox：{subjects}"


async def test_reply_works_when_supervisor_row_is_missing(services, settings) -> None:
    """断点1：supervisors 表里没有这位主管时，靠职位配置的邮箱照样认得出。

    生产环境就是这个状态：三位主管共用 HR 的飞书号，同步后表里只剩一位。
    修之前，这里的回信会被判成陌生人，``feedback.supervisor_unknown`` 后直接丢掉。
    """
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    await _drop_supervisor_row(services)

    outcome = await services.inbound.handle_email(_reply(in_reply_to=message_id))

    assert outcome.handled is True, outcome.detail
    assert outcome.detail.get("action") != "supervisor_unknown"
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED


async def test_new_email_from_supervisor_is_recognised_without_thread(services, settings) -> None:
    """断点2：主管「新写一封」（没有 In-Reply-To）也要认得出——只能靠发件邮箱。"""
    position = await _position(services)
    candidate = await _candidate(services, position)
    await _drop_supervisor_row(services)

    outcome = await services.inbound.handle_email(_reply(in_reply_to=""))

    assert outcome.kind == "supervisor_feedback"
    assert outcome.handled is True, outcome.detail
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED


async def test_reply_token_alias_matches_when_headers_missing(services, settings) -> None:
    """邮箱客户端把 In-Reply-To 改坏时，靠 reply+token 收件地址兜底。"""
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    thread = await _thread_by_root(services, message_id)
    assert thread is not None

    outcome = await services.inbound.handle_email(
        _reply(in_reply_to="", to=(f"reply+{thread.token}@xingye-test.com",))
    )

    assert outcome.handled is True, outcome.detail
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED


# ------------------------------------------------ 带附件的回信（转发/手拖简历）


async def test_reply_with_resume_attachment_still_schedules(services, settings) -> None:
    """主管转发或在回复里又附了份简历：仍按主管指令处理，不重复入库。

    修之前这封邮件会命中「带附件=新投递」，他的指令被丢掉，附件里的简历还会
    再建一个候选人。现在只对「简历包线程 + 带 In-Reply-To」放行，其余不变。
    """
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    before = await _candidate_count(services)

    outcome = await services.inbound.handle_email(
        _reply(in_reply_to=message_id, attachments=[_resume_attachment()])
    )

    assert outcome.kind == "supervisor_feedback", outcome.detail
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED
    assert await _candidate_count(services) == before, "附件里的简历不该被当成新投递再建一个人"


async def test_platform_resume_with_attachment_still_ingests(services, settings) -> None:
    """反向保护：平台共用的通知地址发来新简历，仍然要入库（2026-11-12 修过的 bug）。"""
    before = await _candidate_count(services)

    outcome = await services.inbound.handle_email(
        _reply(
            sender="cv@notice.zhipin.com",
            body="您好，这是候选人李四的简历，请查收。",
            subject="应聘高级后端工程师-李四",
            attachments=[_resume_attachment()],
        )
    )

    assert outcome.kind == "resume", outcome.detail
    assert await _candidate_count(services) == before + 1


async def test_unknown_sender_is_recorded_not_silently_dropped(services) -> None:
    """既认不出人、也不像简历的邮件：留事件与消息，不能无声无息。"""
    outcome = await services.inbound.handle_email(
        _reply(sender="stranger@example.com", body="你好，请问还在招人吗？")
    )

    assert outcome.kind == "unmatched"
    actions = [
        row.action
        for row in (
            await services.session.execute(select(Event).where(Event.action == "inbound.unmatched"))
        ).scalars()
    ]
    assert actions, "至少要留一条 inbound.unmatched，否则排查时无从下手"


async def test_same_reply_twice_is_processed_once(services, settings) -> None:
    """IMAP 水位丢失会把同一封回信重拉一遍，不能因此排两次面试。"""
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    email = _reply(in_reply_to=message_id)

    first = await services.inbound.handle_email(email)
    second = await services.inbound.handle_email(email)

    assert first.handled is True
    assert second.kind == "duplicate"
    assert second.handled is False


# ------------------------------------------- 确认请示：问在他说话的那个渠道里


async def test_email_instruction_asks_for_approval_by_email(services, settings) -> None:
    """闸门开着 + 指令来自邮件 → 请示必须发到邮箱，不能只发飞书。"""
    settings.interview_notice_require_confirm = True
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])

    outcome = await services.inbound.handle_email(_reply(in_reply_to=message_id))

    assert outcome.handled is True
    assert candidate.status == CandidateStatus.INTERVIEW_PENDING_CONFIRM
    subjects = await _outbox_subjects(settings)
    assert any("待确认" in s for s in subjects), f"邮箱里应收到确认请示，实际：{subjects}"
    assert not any("面试通知 |" in s for s in subjects), "确认前不能给候选人发面试通知"


async def test_feishu_instruction_asks_for_approval_by_feishu(services, settings) -> None:
    """闸门开着 + 指令来自飞书 → 请示只发飞书，不在邮箱里多出一封。"""
    settings.interview_notice_require_confirm = True
    position = await _position(services)
    candidate = await _candidate(services, position)

    outcome = await services.feedback.handle(
        InboundMessage(
            channel="feishu",
            sender_id=SUPERVISOR_OPEN_ID,
            sender_name="张伟",
            text=INSTRUCTION,
            message_id=f"om_gate_{uuid.uuid4().hex[:8]}",
            received_at=_now(),
        )
    )

    assert outcome.handled is True, outcome.detail
    assert candidate.status == CandidateStatus.INTERVIEW_PENDING_CONFIRM
    assert not any("待确认" in s for s in await _outbox_subjects(settings))
    feishu_kinds = [
        (row.parsed or {}).get("kind")
        for row in (
            await services.session.execute(
                select(Message).where(
                    Message.channel == Channel.FEISHU, Message.direction == Direction.OUTBOUND
                )
            )
        ).scalars()
    ]
    assert "interview_approval_request" in feishu_kinds


async def test_approval_thread_is_registered_as_supervisor_thread(services, settings) -> None:
    """请示邮件必须登记成主管线程，否则主管回的「确认」会被当成候选人回信。"""
    settings.interview_notice_require_confirm = True
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    await services.inbound.handle_email(_reply(in_reply_to=message_id))

    approval_mail = next(m for m in read_outbox(settings) if "待确认" in m.subject)
    thread = await _thread_by_root(services, approval_mail.message_id)

    assert thread is not None, "请示邮件没登记线程，主管的回信将无法识别"
    assert thread.kind == ThreadKind.INTERVIEW_APPROVAL


async def test_confirm_by_email_sends_notice_and_marks_source(services, settings) -> None:
    """主管在邮箱回「确认」→ 面试通知发出；回执和事件都要标明「邮件」来源。"""
    settings.interview_notice_require_confirm = True
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    await services.inbound.handle_email(_reply(in_reply_to=message_id))

    approval_mail = next(m for m in read_outbox(settings) if "待确认" in m.subject)
    outcome = await services.inbound.handle_email(
        _reply(in_reply_to=approval_mail.message_id, body="确认", subject="Re: 【待确认】1 份面试通知")
    )

    assert outcome.detail.get("action") == "interview_notice_confirmed", outcome.detail
    assert candidate.status == CandidateStatus.INTERVIEW_NOTIFIED
    feishu_texts = [row.body or "" for row in await services.messages.list_outbound_feishu()]
    assert any("邮件" in body for body in feishu_texts), "飞书摘要要写『您在邮件里的确认』"

    confirmed = (
        await services.session.execute(
            select(Event).where(Event.action == "interview.approval_confirmed")
        )
    ).scalars().first()
    assert confirmed is not None
    assert "邮件" in confirmed.note, "事件里要写清是在哪个渠道确认的，否则排查时对不上"
    assert confirmed.detail.get("channel") == Channel.EMAIL


async def test_confirm_result_is_also_emailed_back(services, settings) -> None:
    """主管在邮箱里确认完，邮箱里必须收到回执——否则他以为没生效。"""
    settings.interview_notice_require_confirm = True
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    await services.inbound.handle_email(_reply(in_reply_to=message_id))

    approval_mail = next(m for m in read_outbox(settings) if "待确认" in m.subject)
    await services.inbound.handle_email(
        _reply(in_reply_to=approval_mail.message_id, body="确认", subject="Re: 【待确认】")
    )

    subjects = await _outbox_subjects(settings)
    assert any("处理结果" in s for s in subjects), f"邮箱里应收到结果回执，实际：{subjects}"


async def test_cancel_by_email_reverts_candidate_and_replies(services, settings) -> None:
    """主管回「取消」→ 草稿作废、候选人退回已分发，邮箱收到回执。"""
    settings.interview_notice_require_confirm = True
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])
    await services.inbound.handle_email(_reply(in_reply_to=message_id))

    approval_mail = next(m for m in read_outbox(settings) if "待确认" in m.subject)
    outcome = await services.inbound.handle_email(
        _reply(in_reply_to=approval_mail.message_id, body="取消", subject="Re: 【待确认】")
    )

    assert outcome.detail.get("action") == "interview_notice_cancelled", outcome.detail
    assert candidate.status == CandidateStatus.DISPATCHED
    assert any("处理结果" in s for s in await _outbox_subjects(settings))


# ------------------------------------------------ 自投回流（真机验证时发现）


async def test_own_outbound_email_echo_is_ignored(services, settings) -> None:
    """机器人自己发出的邮件回流到收件箱时，不能当成主管的消息再处理一遍。

    演示环境里主管邮箱就是 HR 邮箱（18786112948@163.com），简历包发出去会绕回同一个
    INBOX。2026-11-14 真机实测：修之前它被判成「主管说了句话」，机器人自问自答一次，
    还给同一个 Message-ID 重复登记了一条线程。
    """
    position = await _position(services)
    candidate = await _candidate(services, position)
    message_id = await _send_package(services, settings, position, [candidate])

    outcome = await services.inbound.handle_email(
        _reply(in_reply_to="", message_id=message_id, body="本轮共收到 1 份简历，附件是压缩包。")
    )

    assert outcome.kind == "self_echo"
    assert outcome.handled is False
    assert candidate.status == CandidateStatus.DISPATCHED, "回流邮件不该改变任何状态"
