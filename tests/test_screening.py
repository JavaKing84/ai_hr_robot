"""筛选规则测试：硬性淘汰、加权打分、阈值、语义加权、落库留痕。"""

from __future__ import annotations

import pytest

from model.candidate import Candidate, CandidateStatus
from repository.position import PositionRepository
from schema.config import ScreeningConfig
from service.screening import ScreeningService, evaluate

BACKEND_SCREENING = {
    "threshold": 60,
    "rule_version": "v1",
    "hard_conditions": [
        {"field": "education_level", "operator": "gte", "value": "本科", "label": "学历本科及以上"},
        {"field": "years_experience", "operator": "gte", "value": 3, "label": "3 年以上经验"},
        {
            "field": "major",
            "operator": "contains_any",
            "value": ["计算机", "软件", "电子信息"],
            "label": "专业对口",
        },
    ],
    "weighted": [
        {
            "field": "skills",
            "operator": "contains_any",
            "weight": 40,
            "value": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "微服务"],
            "label": "技术栈",
        },
        {
            "field": "project_highlights",
            "operator": "contains_any",
            "weight": 25,
            "value": ["高并发", "分布式", "性能优化", "重构"],
            "label": "项目复杂度",
        },
        {"field": "years_experience", "operator": "gte", "weight": 20, "value": 5, "label": "经验年限"},
        {"field": "school", "operator": "exists", "weight": 15, "value": True, "label": "院校信息完整"},
    ],
}

GOOD_CANDIDATE = {
    "education_level": "本科",
    "school": "华北电力大学",
    "major": "计算机科学与技术",
    "years_experience": 6,
    "skills": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "微服务"],
    "project_highlights": ["主导订单服务重构，QPS 提升 3 倍", "设计分布式调度平台"],
}


def _config(**overrides) -> ScreeningConfig:
    payload = {**BACKEND_SCREENING, **overrides}
    return ScreeningConfig.model_validate(payload)


def test_hard_condition_rejects_candidate() -> None:
    result = evaluate({**GOOD_CANDIDATE, "education_level": "大专"}, _config())
    assert result.passed is False
    assert any("学历" in item for item in result.hard_failed)
    assert result.reason.startswith("硬性条件未通过")


def test_full_match_passes_with_expected_score() -> None:
    result = evaluate(GOOD_CANDIDATE, _config())
    assert result.passed is True
    # skills 6/6=40 + projects 2/4*25=12.5 + years 20 + school 15 → 87.5
    assert result.score == pytest.approx(87.5, abs=0.01)
    assert result.rule_version == "v1"


def test_partial_keyword_match_lowers_score() -> None:
    weak = {
        **GOOD_CANDIDATE,
        "skills": ["Python"],
        "project_highlights": ["负责日常需求开发"],
    }
    strong = evaluate(GOOD_CANDIDATE, _config())
    weak_result = evaluate(weak, _config())
    assert weak_result.score < strong.score
    assert weak_result.passed is False


def test_threshold_boundary() -> None:
    config = _config(threshold=88)
    result = evaluate(GOOD_CANDIDATE, config)
    assert result.passed is False
    assert "低于阈值" in result.reason


def test_education_comparison_allows_higher_degree() -> None:
    result = evaluate({**GOOD_CANDIDATE, "education_level": "硕士"}, _config())
    assert result.passed is True


def test_semantic_score_is_weighted() -> None:
    config = _config(semantic_weight=50)
    result = evaluate(GOOD_CANDIDATE, config, semantic_score=0)
    assert result.score == pytest.approx(43.75, abs=0.01)
    assert result.semantic_score == 0


def test_hits_contain_matched_and_missed_keywords() -> None:
    result = evaluate(GOOD_CANDIDATE, _config())
    skill_hit = next(hit for hit in result.hits if hit.field == "skills")
    assert "Python" in skill_hit.matched
    assert skill_hit.missed == []


async def test_screening_service_persists_status_and_event(container, session) -> None:
    positions = PositionRepository(session)
    position = await positions.get_by_code("backend_senior")
    assert position is not None

    candidate = Candidate(
        name="张明",
        email="zhangming@example.com",
        phone="13800001111",
        position_id=position.id,
        position_code=position.code,
        department=position.department,
        education_level=GOOD_CANDIDATE["education_level"],
        school=GOOD_CANDIDATE["school"],
        major=GOOD_CANDIDATE["major"],
        years_experience=GOOD_CANDIDATE["years_experience"],
        skills=GOOD_CANDIDATE["skills"],
        project_highlights=GOOD_CANDIDATE["project_highlights"],
    )
    session.add(candidate)
    await session.flush()

    service = ScreeningService(session, llm=None)
    result = await service.screen_candidate(candidate, position)
    await session.commit()

    assert result.passed is True
    assert candidate.status == CandidateStatus.SCREENED_PASSED
    assert candidate.score > 0
    assert (candidate.score_detail or {}).get("reason")

    from repository.event import EventRepository

    events = await EventRepository(session).list_for_object("candidate", candidate.id)
    assert any(event.action == "screening.passed" for event in events)
