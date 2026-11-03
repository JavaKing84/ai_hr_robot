"""抽取测试：飞书主管消息 ×3、邮件回信 ×3、LLM 结果接地与降级、简历预填。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from client.resume_parser import ResumeParser
from core.exceptions import ExternalServiceError
from core.timeparse import now_utc_naive
from prompt.extraction import RESUME_SYSTEM
from schema.message import InboundMessage
from service.extraction import ExtractionService, detect_intent, match_roster_names
from tests.conftest import resume_text

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 3, 15, 53, tzinfo=TZ)
ROSTER = ["张明", "李四", "王小明", "赵敏", "刘洋", "陈静", "周涛"]


class FakeLLM:
    """可控的 LLM 替身：返回预设 JSON 或抛错，用来验证接地校正与降级。"""

    def __init__(self, responses: list[object], *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.responses = responses
        self.prompts: list[str] = []

    async def complete_json(self, *, system: str, user: str, **_: object) -> dict:
        self.prompts.append(user)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _message(text: str, *, channel: str = "feishu") -> InboundMessage:
    return InboundMessage(
        channel=channel,  # type: ignore[arg-type]
        sender_id="ou_mock_zhangwei" if channel == "feishu" else "zhangwei@xingye-test.com",
        text=text,
        received_at=now_utc_naive(),
        message_id="msg-1",
    )


@pytest.fixture
def service(settings) -> ExtractionService:
    return ExtractionService(settings, llm=None, parser=ResumeParser(settings))


# ------------------------------------------------- 飞书主管消息（3 个用例）


@pytest.mark.parametrize(
    ("text", "expected_names"),
    [
        ("请通知张三、李四，下周三下午14:00来公司面试", ["张三", "李四"]),
        ("我已经筛选审核完毕，请通知王小明 赵敏 于下周三下午14:00点来公司面试", ["王小明", "赵敏"]),
        ("这 12 份我只留了 4 份，请通知刘洋、陈静、周涛 3 人于下周二上午10点到公司面试", ["刘洋", "陈静", "周涛"]),
    ],
)
async def test_extract_interview_schedule_from_feishu(
    service: ExtractionService, text: str, expected_names: list[str]
) -> None:
    roster = [*ROSTER, "张三"]
    schedule = await service.extract_interview_schedule(
        _message(text), roster=roster, position_title="高级后端工程师", now=NOW
    )
    assert schedule.candidate_names == expected_names
    assert schedule.interview_time is not None
    assert schedule.method == "rule"


async def test_schedule_time_is_correct_for_next_wednesday(service: ExtractionService) -> None:
    schedule = await service.extract_interview_schedule(
        _message("请通知张明，下周三下午14:00来公司面试"), roster=ROSTER, now=NOW
    )
    assert schedule.interview_time is not None
    assert schedule.interview_time.date() == datetime(2026, 11, 11).date()
    assert (schedule.interview_time.hour, schedule.interview_time.minute) == (14, 0)


# ------------------------------------------------- 候选人回信（3 个用例）


@pytest.mark.parametrize(
    ("body", "intent"),
    [
        ("您好，我下周二临时有会，能否改到周四下午 3 点？", "reschedule"),
        ("不好意思，我已经找到工作了，这次就不参加了。", "decline"),
        ("收到，我会准时参加面试，谢谢！", "confirm"),
    ],
)
async def test_extract_candidate_reply(service: ExtractionService, body: str, intent: str) -> None:
    reply = await service.extract_candidate_reply(
        body, candidate_name="张明", position_title="高级后端工程师", now=NOW
    )
    assert reply.intent == intent


async def test_reschedule_message_carries_proposed_time(service: ExtractionService) -> None:
    reply = await service.extract_candidate_reply(
        "我下周二临时有会，能否改到周四下午 3 点？", candidate_name="张明", now=NOW
    )
    assert reply.proposed_time is not None
    assert (reply.proposed_time.hour, reply.proposed_time.minute) == (15, 0)


# ------------------------------------------------- LLM 路径：接地 + 降级


async def test_llm_names_are_grounded_to_roster(settings) -> None:
    llm = FakeLLM(
        [
            {
                "candidate_names": ["张明", "查无此人"],
                "interview_time": "2026-11-11T14:00:00+08:00",
                "location": "公司 12 层",
                "confidence": 0.9,
                "source_quotes": [{"field": "candidate_names", "quote": "请通知张明"}],
            }
        ]
    )
    service = ExtractionService(settings, llm=llm, parser=ResumeParser(settings))
    schedule = await service.extract_interview_schedule(
        _message("请通知张明，下周三下午两点来公司面试"), roster=ROSTER, now=NOW
    )
    assert schedule.candidate_names == ["张明"]  # 不在名单里的名字被丢弃
    assert schedule.method == "llm"
    assert schedule.location == "公司 12 层"


async def test_llm_failure_falls_back_to_rules(settings) -> None:
    llm = FakeLLM([ExternalServiceError("模型超时")])
    service = ExtractionService(settings, llm=llm, parser=ResumeParser(settings))
    schedule = await service.extract_interview_schedule(
        _message("请通知张明，下周三下午14:00来公司面试"), roster=ROSTER, now=NOW
    )
    assert schedule.method == "rule"
    assert schedule.candidate_names == ["张明"]


async def test_llm_invalid_schema_falls_back(settings) -> None:
    llm = FakeLLM([{"candidate_names": "不是数组", "interview_time": "上午"}])
    service = ExtractionService(settings, llm=llm, parser=ResumeParser(settings))
    reply = await service.extract_candidate_reply("收到，我会准时参加", candidate_name="张明", now=NOW)
    assert reply.intent == "confirm"
    assert reply.method == "rule"


async def test_llm_can_extract_from_email_channel(settings) -> None:
    llm = FakeLLM(
        [
            {
                "candidate_names": ["张明"],
                "interview_time": "2026-11-12T10:00:00+08:00",
                "location": "",
                "confidence": 0.8,
                "source_quotes": [],
            }
        ]
    )
    service = ExtractionService(settings, llm=llm, parser=ResumeParser(settings))
    schedule = await service.extract_interview_schedule(
        _message("请通知张明，11 月 12 日上午 10 点面试", channel="email"), roster=ROSTER, now=NOW
    )
    assert schedule.interview_time is not None
    assert schedule.interview_time.date() == datetime(2026, 11, 12).date()
    assert "主管消息来源：邮件" in llm.prompts[0]


# ------------------------------------------------- 工具与简历预填


def test_match_roster_names_ignores_noise() -> None:
    text = "请通知张明 李四 下周来面试，技术部会议室"
    assert match_roster_names(text, ROSTER) == ["张明", "李四"]


def test_detect_intent_defaults_to_other() -> None:
    assert detect_intent("附件是我的作品集") == "other"


async def test_resume_prefill_extracts_core_fields(service: ExtractionService) -> None:
    extracted = await service.extract_candidate(resume_text(), positions=[("backend_senior", "高级后端工程师")])
    assert extracted.name == "张明"
    assert extracted.email == "zhangming@example.com"
    assert extracted.phone == "13800001111"
    assert extracted.education_level == "本科"
    assert extracted.school == "华北电力大学"
    assert extracted.major == "计算机科学与技术"
    assert extracted.years_experience == 6
    assert "Python" in extracted.skills
    assert extracted.project_highlights


async def test_resume_extraction_uses_llm_when_available(settings) -> None:
    llm = FakeLLM(
        [
            {
                "name": "李雷",
                "email": "lilei@example.com",
                "phone": "13900002222",
                "education_level": "硕士",
                "school": "清华大学",
                "major": "软件工程",
                "years_experience": 4,
                "skills": ["Go", "Kubernetes"],
                "project_highlights": ["负责调度系统重构"],
                "summary": "四年 Go 后端经验",
                "position_hint": "高级后端工程师",
                "confidence": 0.9,
                "source_quotes": [{"field": "name", "quote": "李雷"}],
            }
        ]
    )
    service = ExtractionService(settings, llm=llm, parser=ResumeParser(settings))
    extracted = await service.extract_candidate("姓名：李雷\n硕士，清华大学软件工程", filename="lilei.txt")
    assert extracted.name == "李雷"
    assert extracted.education_level == "硕士"
    assert "Go" in extracted.skills
    assert len(llm.prompts) == 1
