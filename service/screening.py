"""筛选：硬性条件淘汰 + 加权打分 + 可选语义匹配（对应 prompt/screening.md）。"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from client.llm import LLMClient
from core.exceptions import ExternalServiceError, LLMUnavailable
from core.logging import get_logger, log_extra
from model.candidate import Candidate, CandidateStatus
from model.event import Actor
from model.position import Position
from prompt.extraction import SEMANTIC_SYSTEM, build_semantic_prompt
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from schema.config import HardCondition, ScreeningConfig, WeightedCriterion
from schema.screening import CriterionHit, HardConditionResult, ScreeningResult

logger = get_logger(__name__)

EDUCATION_RANK: dict[str, int] = {
    "": -1,
    "高中": 0,
    "中专": 0,
    "大专": 1,
    "专科": 1,
    "本科": 2,
    "学士": 2,
    "硕士": 3,
    "研究生": 3,
    "博士": 4,
}


def education_rank(level: str | None) -> int:
    return EDUCATION_RANK.get((level or "").strip(), -1)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _normalize(text: Any) -> str:
    return str(text or "").strip().lower()


def _actual(source: Candidate | dict[str, Any], field: str) -> Any:
    if isinstance(source, dict):
        return source.get(field)
    return getattr(source, field, None)


def _compare_numbers(left: Any, right: Any) -> bool | None:
    try:
        return float(left) >= float(right)
    except (TypeError, ValueError):
        return None


def _check_operator(operator: str, actual: Any, expected: Any, field: str) -> tuple[bool, float, list[str], list[str]]:
    """返回 (是否通过, 命中比例, 命中项, 未命中项)。"""
    if operator == "exists":
        wanted = bool(expected) if expected is not None else True
        truthy = bool(actual) and (not isinstance(actual, str) or actual.strip() != "")
        return (truthy == wanted, 1.0 if truthy == wanted else 0.0, [], [])

    if operator in {"gte", "lte"}:
        numeric = _compare_numbers(actual, expected)
        if numeric is None:
            # 学历等有序字段：按等级比较
            if field in {"education_level", "degree"}:
                actual_rank = education_rank(str(actual))
                expected_rank = education_rank(str(expected))
                ok = actual_rank >= expected_rank if operator == "gte" else actual_rank <= expected_rank
                return ok, 1.0 if ok else 0.0, [], []
            return False, 0.0, [], []
        ok = numeric if operator == "gte" else not numeric
        return ok, 1.0 if ok else 0.0, [], []

    if operator in {"eq", "ne"}:
        ok = _normalize(actual) == _normalize(expected)
        if operator == "ne":
            ok = not ok
        return ok, 1.0 if ok else 0.0, [], []

    if operator in {"in", "not_in"}:
        options = [_normalize(item) for item in _as_list(expected)]
        actuals = [_normalize(item) for item in _as_list(actual)]
        hit = any(item in options for item in actuals)
        ok = hit if operator == "in" else not hit
        matched = [item for item in actuals if item in options]
        return ok, 1.0 if ok else 0.0, matched, []

    keywords = _as_list(expected)
    if not keywords:
        return False, 0.0, [], []

    haystack = " ".join(_normalize(item) for item in _as_list(actual))
    matched = [kw for kw in keywords if _normalize(kw) in haystack]
    missed = [kw for kw in keywords if kw not in matched]
    ratio = len(matched) / len(keywords)

    if operator == "contains_all":
        return len(matched) == len(keywords), ratio, matched, missed
    # contains_any（默认）
    return bool(matched), ratio, matched, missed


def evaluate(
    source: Candidate | dict[str, Any],
    screening: ScreeningConfig,
    *,
    semantic_score: float | None = None,
) -> ScreeningResult:
    """纯函数：输入候选人字段 + 职位规则，输出筛选结论（不碰数据库，便于测试）。"""
    hard_results: list[HardConditionResult] = []
    hard_failed: list[str] = []

    for condition in screening.hard_conditions:
        actual = _actual(source, condition.field)
        passed, _, _, _ = _check_operator(condition.operator, actual, condition.value, condition.field)
        label = condition.label or f"{condition.field} {condition.operator} {condition.value}"
        hard_results.append(
            HardConditionResult(
                label=label,
                field=condition.field,
                operator=condition.operator,
                expected=condition.value,
                actual=_stringify(actual),
                passed=passed,
            )
        )
        if not passed:
            hard_failed.append(label)

    hits: list[CriterionHit] = []
    weighted_sum = 0.0
    weight_total = 0.0
    for criterion in screening.weighted:
        actual = _actual(source, criterion.field)
        ok, ratio, matched, missed = _check_operator(
            criterion.operator, actual, criterion.value, criterion.field
        )
        hits.append(
            CriterionHit(
                label=criterion.label or f"{criterion.field} {criterion.operator}",
                field=criterion.field,
                operator=criterion.operator,
                expected=criterion.value,
                actual=_stringify(actual),
                ratio=round(ratio, 4),
                weight=criterion.weight,
                matched=matched,
                missed=missed,
            )
        )
        weighted_sum += ratio * criterion.weight
        weight_total += criterion.weight

    rule_score = round((weighted_sum / weight_total) * 100, 2) if weight_total else 100.0

    final_score = rule_score
    if semantic_score is not None and screening.semantic_weight > 0:
        weight = min(max(screening.semantic_weight, 0), 100) / 100
        final_score = round(rule_score * (1 - weight) + semantic_score * weight, 2)

    passed = not hard_failed and final_score >= screening.threshold
    if hard_failed:
        reason = "硬性条件未通过：" + "；".join(hard_failed)
    elif passed:
        reason = f"通过筛选（{final_score:.1f} 分 / 阈值 {screening.threshold:.0f}）"
    else:
        reason = f"加权得分 {final_score:.1f} 低于阈值 {screening.threshold:.0f}"

    return ScreeningResult(
        passed=passed,
        score=min(max(final_score, 0.0), 100.0),
        threshold=screening.threshold,
        rule_version=screening.rule_version,
        hard_conditions=hard_results,
        hard_failed=hard_failed,
        hits=hits,
        semantic_score=semantic_score,
        reason=reason,
    )


def _stringify(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value)
    return value


class ScreeningService:
    """在 `evaluate` 之上补上「落库 + 留痕 + 可选语义分」。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        llm: LLMClient | None = None,
        company_name: str = "",
    ) -> None:
        self.session = session
        self.llm = llm
        self.company_name = company_name
        self.candidates = CandidateRepository(session)
        self.events = EventRepository(session)

    async def _semantic_score(self, candidate: Candidate, position: Position) -> float | None:
        screening = ScreeningConfig.model_validate(position.screening or {})
        if screening.semantic_weight <= 0 or self.llm is None or not self.llm.enabled:
            return None
        try:
            data = await self.llm.complete_json(
                system=SEMANTIC_SYSTEM,
                user=build_semantic_prompt(
                    company_name=self.company_name or "公司",
                    position_title=position.title,
                    position_description=position.description,
                    resume_summary=candidate.summary or candidate.resume_text[:500],
                    skills=list(candidate.skills or []),
                    project_highlights=list(candidate.project_highlights or []),
                ),
            )
            score = float(data.get("semantic_score", 0))
            return min(max(score, 0.0), 100.0)
        except (ExternalServiceError, LLMUnavailable) as exc:
            logger.warning(
                "screening.semantic_unavailable",
                extra=log_extra(candidate_id=candidate.id, error=str(exc)),
            )
            return None

    async def screen_candidate(self, candidate: Candidate, position: Position) -> ScreeningResult:
        screening = ScreeningConfig.model_validate(position.screening or {})
        semantic = await self._semantic_score(candidate, position)
        result = evaluate(candidate, screening, semantic_score=semantic)

        from_state = candidate.status
        candidate.score = result.score
        candidate.score_detail = result.model_dump(mode="json")
        candidate.hard_failed = result.hard_failed
        candidate.status = (
            CandidateStatus.SCREENED_PASSED if result.passed else CandidateStatus.SCREENED_REJECTED
        )
        await self.session.flush()

        await self.events.record(
            "screening.passed" if result.passed else "screening.rejected",
            actor=Actor.ROBOT,
            object_type="candidate",
            object_id=candidate.id,
            from_state=from_state,
            to_state=candidate.status,
            detail={
                "position_code": position.code,
                "score": result.score,
                "threshold": result.threshold,
                "rule_version": result.rule_version,
                "hard_failed": result.hard_failed,
                "semantic_score": result.semantic_score,
            },
            note=result.reason,
        )
        return result


async def screen_all(
    session: AsyncSession,
    candidates: Iterable[Candidate],
    positions: dict[int, Position],
    *,
    llm: LLMClient | None = None,
    company_name: str = "",
) -> dict[str, int]:
    service = ScreeningService(session, llm=llm, company_name=company_name)
    passed = rejected = skipped = 0
    for candidate in candidates:
        position = positions.get(candidate.position_id or -1)
        if position is None:
            skipped += 1
            continue
        result = await service.screen_candidate(candidate, position)
        if result.passed:
            passed += 1
        else:
            rejected += 1
    return {"passed": passed, "rejected": rejected, "skipped": skipped}


__all__ = ["EDUCATION_RANK", "ScreeningService", "education_rank", "evaluate", "screen_all"]
