"""筛选：硬性条件淘汰 + 加权打分 + 可选语义匹配（对应 prompt/screening.md）。"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from client.llm import LLMClient
from config.settings import Settings
from core.logging import get_logger, log_extra
from model.candidate import Candidate, CandidateStatus
from model.event import Actor
from model.position import Position
from prompt.extraction import (
    DEEP_ANALYSIS_SYSTEM,
    SEMANTIC_SYSTEM,
    build_deep_prompt,
    build_semantic_prompt,
)
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

#: 这些字段「抽不到」和「不符合」是两件事：抽取为空时跳过淘汰、转人工复核。
#: 2026-11-12 实测：BOSS 简历 16 份里有 8 份因「专业」被硬挂，其中多数是
#: 正则把「提升的专业精神」当成了专业——用错数据做 0/1 淘汰比不判更糟。
#: ``education_level`` 不在此列：学历正则命中率极高，抽不到基本是图片型 PDF。
UNKNOWN_AWARE_FIELDS = frozenset({"major", "school"})


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


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
    semantic_applied: bool = True,
    semantic_note: str = "",
    deep_analysis: dict[str, Any] | None = None,
) -> ScreeningResult:
    """纯函数：输入候选人字段 + 职位规则，输出筛选结论（不碰数据库，便于测试）。

    ``semantic_applied=False`` 时只把语义分记进结果、不参与总分（影子模式），
    这样同一批简历可以同时看到「规则判定」和「融合判定」两种结果。
    ``deep_analysis`` 只做展示与留痕，不参与判定（防止模型一句话改变录用结果）。
    """
    # 白名单：主测试简历等指定候选人跳过全部筛选（硬性条件/打分/阈值）直接通过
    name = str(_actual(source, "name") or "").strip()
    whitelist = {str(n).strip() for n in screening.always_pass_names if str(n).strip()}
    if name and name in whitelist:
        return ScreeningResult(
            passed=True,
            score=100.0,
            threshold=screening.threshold,
            rule_version=screening.rule_version,
            reason=f"白名单命中（{name}），跳过筛选直接通过",
        )

    hard_results: list[HardConditionResult] = []
    hard_failed: list[str] = []
    hard_unknown: list[str] = []

    for condition in screening.hard_conditions:
        actual = _actual(source, condition.field)
        label = condition.label or f"{condition.field} {condition.operator} {condition.value}"
        if condition.field in UNKNOWN_AWARE_FIELDS and _is_blank(actual):
            # 字段抽取为空 → 不知道，不等于不符合。跳过淘汰并提示人工复核，
            # 否则「正则没抽到」会被当成「专业不对口」，直接把合格候选人毙掉。
            hard_results.append(
                HardConditionResult(
                    label=label,
                    field=condition.field,
                    operator=condition.operator,
                    expected=condition.value,
                    actual="未知",
                    passed=True,
                    unknown=True,
                )
            )
            hard_unknown.append(label)
            continue

        passed, _, _, _ = _check_operator(condition.operator, actual, condition.value, condition.field)
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
        if criterion.field in UNKNOWN_AWARE_FIELDS and _is_blank(actual):
            # 缺数据不参与加权分母，否则「专业没抽到」会被当成「专业不相关」，
            # 白扣 15 分。分母同步缩小，等价于按其余项的平均水平折算。
            continue
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

    # 融合：只有「语义分拿到」+「岗位配了权重」+「非影子模式」三个条件同时成立才算分。
    applied = False
    final_score = rule_score
    if semantic_score is not None and screening.semantic_weight > 0 and semantic_applied:
        weight = min(max(screening.semantic_weight, 0), 100) / 100
        final_score = round(rule_score * (1 - weight) + semantic_score * weight, 2)
        applied = True

    passed = not hard_failed and final_score >= screening.threshold
    if hard_failed:
        reason = "硬性条件未通过：" + "；".join(hard_failed)
    elif passed:
        reason = f"通过筛选（{final_score:.1f} 分 / 阈值 {screening.threshold:.0f}）"
        if applied:
            reason += (
                f"（规则分 {rule_score:.1f} + 语义分 {semantic_score:.1f}"
                f"，语义权重 {screening.semantic_weight:.0f}%）"
            )
        elif semantic_score is not None:
            reason += f"（语义分 {semantic_score:.1f} 仅记录，未参与判定）"
        if hard_unknown:
            reason += f"；{len(hard_unknown)} 项字段缺失未参与淘汰，建议人工复核"
    else:
        reason = f"加权得分 {final_score:.1f} 低于阈值 {screening.threshold:.0f}"
        if applied:
            reason += f"（规则分 {rule_score:.1f} + 语义分 {semantic_score:.1f}）"
        if hard_unknown:
            reason += f"（其中 {'、'.join(hard_unknown)} 因数据缺失未参与淘汰）"

    return ScreeningResult(
        passed=passed,
        score=min(max(final_score, 0.0), 100.0),
        threshold=screening.threshold,
        rule_version=screening.rule_version,
        hard_conditions=hard_results,
        hard_failed=hard_failed,
        hard_unknown=hard_unknown,
        hits=hits,
        rule_score=rule_score,
        semantic_score=semantic_score,
        semantic_applied=applied,
        semantic_note=semantic_note,
        deep_analysis=deep_analysis or {},
        reason=reason,
    )


def _stringify(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value)
    return value


_DIMENSION_KEYS = ("skill_match", "experience_depth", "delivery_ability", "growth_potential")
_DIMENSION_LABELS = {
    "skill_match": "技术栈匹配",
    "experience_depth": "经验深度",
    "delivery_ability": "交付能力",
    "growth_potential": "成长潜力",
}


def _normalize_deep(data: dict[str, Any]) -> dict[str, Any]:
    """把模型返回的深度解析归一到可入库的结构，脏数据一律丢弃而不是原样落库。

    模型偶尔会把维度写成字符串、把 strengths 写成一句话、把分数写成 120。
    这里做三件事：维度分夹到 0-100、列表项裁到 3 条且非空、缺字段补空。
    """
    if not isinstance(data, dict) or not data.get("dimensions") and not data.get("strengths"):
        return {}

    raw_dims = data.get("dimensions")
    dims: dict[str, float] = {}
    if isinstance(raw_dims, dict):
        for key in _DIMENSION_KEYS:
            try:
                dims[key] = round(min(max(float(raw_dims.get(key) or 0), 0.0), 100.0), 1)
            except (TypeError, ValueError):
                dims[key] = 0.0

    def _list(field: str, limit: int = 3) -> list[str]:
        value = data.get(field)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip()[:60] for item in value if str(item).strip()][:limit]

    result: dict[str, Any] = {
        "dimensions": dims,
        "dimension_labels": {key: _DIMENSION_LABELS[key] for key in dims},
        "strengths": _list("strengths"),
        "risks": _list("risks"),
        "interview_focus": _list("interview_focus"),
        "reason": str(data.get("reason") or "").strip()[:120],
    }
    return result if (dims or result["strengths"] or result["risks"]) else {}


class ScreeningService:
    """在 `evaluate` 之上补上「落库 + 留痕 + 可选语义分」。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        llm: LLMClient | None = None,
        company_name: str = "",
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.llm = llm
        self.company_name = company_name
        self.settings = settings
        self.candidates = CandidateRepository(session)
        self.events = EventRepository(session)

    @property
    def semantic_shadow(self) -> bool:
        """没有 settings（单测/脚本直接构造）时按「非影子」处理，保持旧行为。"""
        return bool(self.settings.semantic_shadow) if self.settings is not None else False

    @property
    def deep_enabled(self) -> bool:
        """是否启用大模型深度解析（分维度评分 + 亮点/风险/面试追问点）。"""
        return bool(self.settings.llm_deep_analysis) if self.settings is not None else False

    async def _semantic_score(
        self, candidate: Candidate, position: Position
    ) -> tuple[float | None, str, dict[str, Any]]:
        """调用大模型给「语义分 + 深度解析」，返回 ``(分数, 说明, 深度解析)``。

        任何异常都只降级、不抛出——语义分与深度解析都是「加分项」，
        不能把整轮入库带崩。LLM 三家全挂时返回 ``(None, 原因, {})``，
        调用方照常按规则分判定，业务不感知。
        """
        screening = ScreeningConfig.model_validate(position.screening or {})
        want_semantic = screening.semantic_weight > 0
        deep = self.deep_enabled
        if not want_semantic and not deep:
            return None, "岗位未配置 semantic_weight 且未开启深度解析，只按规则打分", {}
        if self.llm is None or not self.llm.enabled:
            return None, "LLM 未启用（LLM_MODE=off 或缺少可用供应商）", {}

        # 深度解析与语义分共用一次调用：同一份简历不重复烧 token。
        system = DEEP_ANALYSIS_SYSTEM if deep else SEMANTIC_SYSTEM
        if deep:
            prompt = build_deep_prompt(
                company_name=self.company_name or "公司",
                position_title=position.title,
                position_description=position.description,
                resume_text=candidate.summary or candidate.resume_text[:6000],
                skills=list(candidate.skills or []),
                project_highlights=list(candidate.project_highlights or []),
                education_level=candidate.education_level or "",
                school=candidate.school or "",
                major=candidate.major or "",
                years_experience=float(candidate.years_experience or 0),
            )
        else:
            prompt = build_semantic_prompt(
                company_name=self.company_name or "公司",
                position_title=position.title,
                position_description=position.description,
                resume_summary=candidate.summary or candidate.resume_text[:1500],
                skills=list(candidate.skills or []),
                project_highlights=list(candidate.project_highlights or []),
                education_level=candidate.education_level or "",
                school=candidate.school or "",
                major=candidate.major or "",
                years_experience=float(candidate.years_experience or 0),
            )

        data: dict[str, Any] | None = None
        try:
            # 优先用 safe_complete_json（三家供应商全挂也只返回 None，不抛异常）；
            # 测试用的假 LLM 只实现了 complete_json，这里做一次能力探测，
            # 免得「为了可测试性」把静默兜底写死成唯一入口。
            caller = getattr(self.llm, "safe_complete_json", None)
            if callable(caller):
                data = await caller(system=system, user=prompt, max_tokens=1800)
            else:
                data = await self.llm.complete_json(system=system, user=prompt, max_tokens=1800)
        except Exception as exc:  # noqa: BLE001 - 双保险，绝不让打分拖垮整轮
            logger.warning(
                "screening.deep_unavailable",
                extra=log_extra(candidate_id=candidate.id, error=str(exc)),
            )
            data = None

        if not data:
            return None, "语义分/深度解析调用失败，已降级为规则分", {}

        raw = data.get("semantic_score")
        if raw is None:
            return None, "模型未返回 semantic_score 字段", {}
        try:
            score = min(max(float(raw), 0.0), 100.0)
        except (TypeError, ValueError):
            logger.warning(
                "screening.semantic_invalid", extra=log_extra(candidate_id=candidate.id, raw=str(raw))
            )
            return None, f"semantic_score 无法解析：{raw!r}", {}

        deep_result = _normalize_deep(data)
        # 全 0 且四个维度也全 0：极可能是模型把提示词里的示例 JSON 原样抄了回来
        # （实测学院 qwen3.6 的 reasoning 里就出现过）。这种结果没有信息量，
        # 当成「不可用」退回规则分，而不是给候选人记一个 0 分的语义分。
        if score <= 0 and deep_result and not any((deep_result.get("dimensions") or {}).values()):
            logger.warning(
                "screening.deep_all_zero",
                extra=log_extra(candidate_id=candidate.id, provider=getattr(self.llm, "last_provider", "")),
            )
            return None, "模型返回全 0（疑似照抄示例模板），已降级为规则分", {}

        return score, str(data.get("reason") or "").strip()[:120], deep_result

    async def screen_candidate(self, candidate: Candidate, position: Position) -> ScreeningResult:
        screening = ScreeningConfig.model_validate(position.screening or {})

        # 第一步：只跑硬条件 + 规则分。纯本地计算，不花钱。
        pre = evaluate(candidate, screening)

        # 第二步：硬条件都过了才值得调大模型（淘汰的人没必要花这一次调用）。
        semantic: float | None = None
        note = ""
        applied = False
        deep: dict[str, Any] = {}
        if pre.hard_failed:
            note = "硬性条件未通过，跳过语义分"
        else:
            semantic, note, deep = await self._semantic_score(candidate, position)
            applied = semantic is not None and not self.semantic_shadow
            if semantic is not None and self.semantic_shadow:
                note = (note + " " if note else "") + "影子模式：语义分仅记录，未参与判定"

        result = evaluate(
            candidate,
            screening,
            semantic_score=semantic,
            semantic_applied=applied,
            semantic_note=note,
            deep_analysis=deep,
        )

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
                "rule_score": result.rule_score,
                "threshold": result.threshold,
                "rule_version": result.rule_version,
                "hard_failed": result.hard_failed,
                "hard_unknown": result.hard_unknown,
                "semantic_score": result.semantic_score,
                "semantic_applied": result.semantic_applied,
                "semantic_note": result.semantic_note,
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
    settings: Settings | None = None,
) -> dict[str, int]:
    service = ScreeningService(session, llm=llm, company_name=company_name, settings=settings)
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
