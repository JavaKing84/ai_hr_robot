"""筛选结果 schema：硬性条件明细 + 加权命中明细 + 综合分（产品文档第 4 节）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HardConditionResult(BaseModel):
    label: str = ""
    field: str
    operator: str
    expected: Any = None
    actual: Any = None
    passed: bool = True


class CriterionHit(BaseModel):
    label: str = ""
    field: str
    operator: str
    expected: Any = None
    actual: Any = None
    #: 命中比例 0-1（关键词类条件可以是部分命中）
    ratio: float = Field(default=0.0, ge=0, le=1)
    weight: float = 0.0
    matched: list[str] = Field(default_factory=list)
    missed: list[str] = Field(default_factory=list)


class ScreeningResult(BaseModel):
    passed: bool
    score: float = Field(ge=0, le=100)
    threshold: float
    rule_version: str = "v1"
    hard_conditions: list[HardConditionResult] = Field(default_factory=list)
    hard_failed: list[str] = Field(default_factory=list)
    hits: list[CriterionHit] = Field(default_factory=list)
    semantic_score: float | None = None
    reason: str = ""

    def summary(self) -> str:
        if self.passed:
            return f"通过筛选（{self.score:.1f} 分 / 阈值 {self.threshold:.0f}）"
        if self.hard_failed:
            return "硬性条件未通过：" + "；".join(self.hard_failed)
        return f"加权得分 {self.score:.1f} 低于阈值 {self.threshold:.0f}"
