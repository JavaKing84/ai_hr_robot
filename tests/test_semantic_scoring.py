"""语义分链路测试：硬条件先跑、影子模式、融合公式、降级、字段合并策略。

外部依赖全部用假对象：LLM 用 ``_StubLLM``，邮件/飞书不参与。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.exceptions import ExternalServiceError
from model.candidate import Candidate
from repository.position import PositionRepository
from schema.config import ScreeningConfig
from schema.extraction import ExtractedCandidate
from service.extraction import _merge_resume
from service.screening import ScreeningService, evaluate

BASE_CONFIG = {
    "threshold": 60,
    "semantic_weight": 30,
    "weighted": [
        {"field": "skills", "operator": "contains_any", "weight": 100, "value": ["Python"]},
    ],
}

FULL_CANDIDATE = {
    "education_level": "本科",
    "school": "华北电力大学",
    "major": "计算机科学与技术",
    "years_experience": 6,
    "skills": ["Python"],
}


class _StubLLM:
    """冒充 LLMClient：只实现筛选服务用到的两个成员。"""

    def __init__(self, score: float = 90.0, *, boom: bool = False) -> None:
        self.score = score
        self.boom = boom
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    async def complete_json(self, *, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.boom:
            raise ExternalServiceError("模拟模型超时")
        return {"semantic_score": self.score, "reason": "技术栈与 JD 基本吻合"}


def _config(**overrides) -> ScreeningConfig:
    return ScreeningConfig.model_validate({**BASE_CONFIG, **overrides})


# --------------------------------------------------------------- 纯函数：evaluate


def test_shadow_keeps_rule_score_but_records_semantic() -> None:
    result = evaluate(FULL_CANDIDATE, _config(), semantic_score=95, semantic_applied=False)
    assert result.rule_score == pytest.approx(100.0)
    assert result.score == pytest.approx(100.0)  # 影子模式不融合
    assert result.semantic_score == 95
    assert result.semantic_applied is False
    assert "未参与判定" in result.reason


def test_fusion_formula_with_30_percent_weight() -> None:
    result = evaluate(FULL_CANDIDATE, _config(), semantic_score=50)
    # 100 * 0.7 + 50 * 0.3 = 85
    assert result.score == pytest.approx(85.0)
    assert result.semantic_applied is True
    assert result.rule_score == pytest.approx(100.0)


def test_zero_weight_never_fuses() -> None:
    result = evaluate(FULL_CANDIDATE, _config(semantic_weight=0), semantic_score=0)
    assert result.score == pytest.approx(100.0)
    assert result.semantic_applied is False


# ------------------------------------------------------------ 服务：硬条件先跑


async def _candidate(session, position, *, degree: str = "本科") -> Candidate:
    candidate = Candidate(
        name="测试候选人",
        email="semantic_test@example.com",
        phone="13800002222",
        position_id=position.id,
        position_code=position.code,
        department=position.department,
        education_level=degree,
        school="华北电力大学",
        major="计算机科学与技术",
        years_experience=6,
        skills=["Python", "FastAPI"],
        project_highlights=["订单服务重构"],
        resume_text="（测试用正文）",
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def test_hard_failed_candidate_skips_llm(container, session, settings) -> None:
    position = await PositionRepository(session).get_by_code("backend_senior")
    stub = _StubLLM(95)
    service = ScreeningService(session, llm=stub, company_name="测试公司", settings=settings)

    candidate = await _candidate(session, position, degree="大专")
    result = await service.screen_candidate(candidate, position)

    assert result.passed is False
    assert stub.calls == 0, "硬条件不过不该调用大模型"
    assert result.semantic_score is None
    assert "跳过语义分" in result.semantic_note


async def test_shadow_mode_does_not_change_verdict(container, session, settings) -> None:
    position = await PositionRepository(session).get_by_code("backend_senior")
    settings.semantic_shadow = True
    stub = _StubLLM(10)  # 语义分极低，若生效会把合格候选人拉下去
    service = ScreeningService(session, llm=stub, company_name="测试公司", settings=settings)

    candidate = await _candidate(session, position)
    result = await service.screen_candidate(candidate, position)

    assert result.semantic_score == 10
    assert result.semantic_applied is False
    assert result.score == pytest.approx(result.rule_score)
    assert result.passed is (result.rule_score >= result.threshold)


async def test_llm_failure_degrades_to_rule_score(container, session, settings) -> None:
    position = await PositionRepository(session).get_by_code("backend_senior")
    settings.semantic_shadow = False
    stub = _StubLLM(boom=True)
    service = ScreeningService(session, llm=stub, company_name="测试公司", settings=settings)

    candidate = await _candidate(session, position)
    result = await service.screen_candidate(candidate, position)  # 不应抛异常

    assert result.semantic_score is None
    assert result.score == pytest.approx(result.rule_score)
    assert "降级" in result.semantic_note


async def test_fusion_applied_when_shadow_off(container, session, settings) -> None:
    position = await PositionRepository(session).get_by_code("backend_senior")
    settings.semantic_shadow = False
    stub = _StubLLM(100)
    service = ScreeningService(session, llm=stub, company_name="测试公司", settings=settings)

    candidate = await _candidate(session, position)
    result = await service.screen_candidate(candidate, position)

    weight = (position.screening or {}).get("semantic_weight", 0) / 100
    expected = round(result.rule_score * (1 - weight) + 100 * weight, 2)
    assert result.semantic_applied is True
    assert result.score == pytest.approx(expected, abs=0.01)
    assert (candidate.score_detail or {}).get("rule_score") is not None


# --------------------------------------------------------- 抽取合并：规则优先


def _extracted(**overrides) -> ExtractedCandidate:
    payload = {
        "name": "",
        "email": "",
        "phone": "",
        "education_level": "",
        "school": "",
        "major": "",
        "years_experience": 0,
        "skills": [],
        "project_highlights": [],
        "summary": "",
        "position_hint": "",
        "confidence": 0.5,
    }
    payload.update(overrides)
    return ExtractedCandidate.model_validate(payload)


def test_merge_prefers_rule_for_basic_fields() -> None:
    rule = _extracted(
        name="李四",
        email="lisi@example.com",
        education_level="本科",
        school="华中科技大学",
        major="计算机科学与技术",
        years_experience=5.5,
        skills=["Python"],
    )
    llm = _extracted(
        name="李四先生",
        email="lisi.wrong@example.com",
        education_level="硕士",
        years_experience=9,
        skills=["FastAPI"],
        summary="5 年后端经验",
        position_hint="Python开发工程师",
    )

    merged = _merge_resume(rule, llm)
    assert merged.name == "李四"
    assert merged.email == "lisi@example.com"
    assert merged.education_level == "本科"
    assert merged.years_experience == 5.5
    # 复杂字段交给模型
    assert merged.summary == "5 年后端经验"
    assert merged.position_hint == "Python开发工程师"
    assert merged.skills[:2] == ["FastAPI", "Python"]


def test_merge_uses_llm_years_only_when_rule_is_zero() -> None:
    rule = _extracted(name="王五", years_experience=0)
    llm = _extracted(years_experience=3)
    assert _merge_resume(rule, llm).years_experience == 3
