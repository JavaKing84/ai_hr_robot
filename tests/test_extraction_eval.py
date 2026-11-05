"""抽取量化评估测试（§4-A.2 / §7.3）：复用 scripts/eval_extraction.EVAL_CASES。

这些测试**只保护硬不变量**（姓名接地 I-05、名单精确匹配、回信意图识别），
不断言时间字段——时间准确性由 eval_extraction.py 量化报告呈现，避免把"规则路径的
已知弱项"误判为测试失败。全部离线、确定性、独立。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from client.resume_parser import ResumeParser
from schema.message import InboundMessage
from scripts.eval_extraction import EVAL_CASES, ROSTER
from service.extraction import ExtractionService

_NOW = datetime(2026, 11, 3, 15, 53, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def service(settings) -> ExtractionService:
    # 显式不传 LLM → 规则主路径（与 eval 脚本一致，离线可复现）
    return ExtractionService(settings, llm=None, parser=ResumeParser(settings))


def _message(text: str) -> InboundMessage:
    return InboundMessage(
        channel="feishu",  # type: ignore[arg-type]
        sender_id="ou_eval",
        text=text,
        received_at=_NOW.replace(tzinfo=None),
        message_id="eval-msg",
    )


def test_eval_set_has_enough_cases() -> None:
    """评估集规模达标（§4-A.2 要求 ≥10 条人工标注）。"""
    assert len(EVAL_CASES) >= 10


@pytest.mark.parametrize("case", EVAL_CASES, ids=lambda c: f"{c['kind']}:{c['note']}")
async def test_names_are_grounded_to_roster(service: ExtractionService, case: dict) -> None:
    """I-05 姓名接地：无论抽取结果如何，出现在候选人名单里的姓名必须全部来自名单，
    绝不允许把'12份''小李'之类非名单词当人名。"""
    if case["kind"] != "interview":
        return  # 接地只针对主管安排里的候选人姓名
    sched = await service.extract_interview_schedule(
        _message(case["text"]), roster=ROSTER, now=_NOW
    )
    assert sched.candidate_names, "应至少抽到一名候选人"
    for name in sched.candidate_names:
        assert name in ROSTER, f"非名单姓名被当作候选人：{name!r}"


@pytest.mark.parametrize("case", EVAL_CASES, ids=lambda c: f"{c['kind']}:{c['note']}")
async def test_interview_names_exact_match(service: ExtractionService, case: dict) -> None:
    """名单精确匹配：规则路径应能从文本里抽出与人工标注完全一致的候选人集合
    （含'12份/3人'噪声不误判、名单外'小李'被排除）。"""
    if case["kind"] != "interview":
        return
    sched = await service.extract_interview_schedule(
        _message(case["text"]), roster=ROSTER, now=_NOW
    )
    assert set(sched.candidate_names) == set(case["expected_names"])


@pytest.mark.parametrize("case", EVAL_CASES, ids=lambda c: f"{c['kind']}:{c['note']}")
async def test_reply_intent_exact_match(service: ExtractionService, case: dict) -> None:
    """回信意图识别：confirm / reschedule / decline 必须与人工标注一致。"""
    if case["kind"] != "reply":
        return
    reply = await service.extract_candidate_reply(
        case["text"], candidate_name=case.get("expected_names", [""])[0], now=_NOW
    )
    assert reply.intent == case["expected_intent"]
