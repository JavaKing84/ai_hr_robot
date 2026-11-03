"""邮件线程测试：In-Reply-To / References 命中、token 兜底、发件人兜底、树构建。"""

from __future__ import annotations

from datetime import datetime

from client.mailbox import InboundEmail
from core.threading import MessageThreadHeaders, build_threads, root_of
from model.candidate import Candidate
from model.email_thread import ThreadKind
from repository.email_thread import EmailThreadRepository
from repository.position import PositionRepository
from service.email_threading import EmailThreadService, extract_reply_token


def _email(**overrides) -> InboundEmail:
    base = dict(
        message_id="<in-1@example.com>",
        subject="回复：面试通知",
        from_address="zhangming@example.com",
        to_addresses=["hr@xingye-test.com"],
        body_text="好的",
        received_at=datetime(2026, 11, 4, 10, 0),
    )
    base.update(overrides)
    return InboundEmail(**base)


def test_build_threads_groups_replies() -> None:
    messages = [
        MessageThreadHeaders(message_id="a@x"),
        MessageThreadHeaders(message_id="b@x", in_reply_to="a@x", references=("a@x",)),
        MessageThreadHeaders(message_id="c@x", references=("a@x", "b@x")),
    ]
    threads = build_threads(messages)
    assert list(threads.keys()) == ["a@x"]
    assert sorted(threads["a@x"]) == ["a@x", "b@x", "c@x"]


def test_root_of_survives_cycle() -> None:
    headers = {
        "a@x": MessageThreadHeaders(message_id="a@x", in_reply_to="b@x"),
        "b@x": MessageThreadHeaders(message_id="b@x", in_reply_to="a@x"),
    }
    assert root_of(headers, "a@x") in {"a@x", "b@x"}


def test_extract_reply_token() -> None:
    assert extract_reply_token(["reply+abc123@xingye-test.com"]) == "abc123"
    assert extract_reply_token(["hr@xingye-test.com"]) == ""


async def test_match_by_in_reply_to_for_supervisor(container, session) -> None:
    positions = PositionRepository(session)
    position = await positions.get_by_code("backend_senior")
    assert position is not None

    threads = EmailThreadRepository(session)
    thread = await threads.create(
        token="tok-supervisor",
        root_message_id="<pkg@xingye-test.com>",
        kind=ThreadKind.RESUME_PACKAGE,
        subject="【简历筛选】技术部",
        counterpart_email="zhangwei@xingye-test.com",
        position_id=position.id,
    )
    await session.commit()

    service = EmailThreadService(session, container.settings)
    match = await service.match(
        _email(
            message_id="<reply-1@xingye-test.com>",
            from_address="zhangwei@xingye-test.com",
            in_reply_to="<pkg@xingye-test.com>",
            references=["<pkg@xingye-test.com>"],
        )
    )
    assert match.role == "supervisor"
    assert match.thread is not None and match.thread.token == thread.token
    assert match.position is not None and match.position.code == "backend_senior"


async def test_match_by_token_when_headers_missing(container, session) -> None:
    threads = EmailThreadRepository(session)
    thread = await threads.create(
        token="fallback001",
        root_message_id="<pkg2@xingye-test.com>",
        kind=ThreadKind.RESUME_PACKAGE,
        subject="简历包",
        counterpart_email="zhangwei@xingye-test.com",
    )
    await session.commit()

    service = EmailThreadService(session, container.settings)
    match = await service.match(
        _email(
            message_id="<unknown@x>",
            from_address="someone-else@example.com",
            to_addresses=["reply+fallback001@xingye-test.com"],
        )
    )
    assert match.thread is not None and match.thread.token == thread.token
    assert "token" in match.reason


async def test_match_candidate_by_references(container, session) -> None:
    candidate = Candidate(name="张明", email="zhangming@example.com", phone="13800001111")
    session.add(candidate)
    await session.flush()

    threads = EmailThreadRepository(session)
    await threads.create(
        token="tok-candidate",
        root_message_id="<notice@xingye-test.com>",
        kind=ThreadKind.INTERVIEW_NOTICE,
        subject="面试通知",
        counterpart_email="zhangming@example.com",
        candidate_id=candidate.id,
    )
    await session.commit()

    service = EmailThreadService(session, container.settings)
    match = await service.match(
        _email(
            message_id="<reply-2@example.com>",
            in_reply_to="<notice@xingye-test.com>",
            references=["<notice@xingye-test.com>"],
        )
    )
    assert match.role == "candidate"
    assert match.candidate is not None and match.candidate.id == candidate.id


async def test_match_by_sender_email_fallback(container, session) -> None:
    service = EmailThreadService(session, container.settings)
    match = await service.match(
        _email(message_id="<no-header@x>", from_address="zhangwei@xingye-test.com")
    )
    assert match.role == "supervisor"
    assert "发件人邮箱" in match.reason


async def test_unknown_sender(container, session) -> None:
    service = EmailThreadService(session, container.settings)
    match = await service.match(_email(message_id="<x@y>", from_address="stranger@nowhere.com"))
    assert match.role == "unknown"
