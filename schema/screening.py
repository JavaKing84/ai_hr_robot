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
    #: 字段抽取为空（不是「不符合」，而是「不知道」）。这类条件不参与淘汰，
    #: 只提示人工复核——避免「正则没抽到专业」被当成「专业不对口」直接毙掉。
    unknown: bool = False


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
    #: 因字段缺失而跳过淘汰的条件（转人工复核，不算未通过）
    hard_unknown: list[str] = Field(default_factory=list)
    hits: list[CriterionHit] = Field(default_factory=list)
    #: 纯规则分（不含语义分），用于对比融合前后的变化
    rule_score: float = 0.0
    semantic_score: float | None = None
    #: 语义分是否真正参与了总分计算（影子模式下算过但不参与，为 False）
    semantic_applied: bool = False
    #: 语义分说明：不可用原因（超时/无 key/权重为 0），或模型给出的评分理由
    semantic_note: str = ""
    #: 大模型深度解析结果：{dimensions, strengths, risks, interview_focus, provider}
    #: 为空表示「这一份没跑深度解析」（LLM 不可用 / 未开启 / 硬条件已淘汰）
    deep_analysis: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""

    def summary(self) -> str:
        if self.passed:
            text = f"通过筛选（{self.score:.1f} 分 / 阈值 {self.threshold:.0f}）"
            if self.hard_unknown:
                text += f"；{len(self.hard_unknown)} 项字段缺失未参与淘汰，建议人工复核"
            return text
        if self.hard_failed:
            return "硬性条件未通过：" + "；".join(self.hard_failed)
        return f"加权得分 {self.score:.1f} 低于阈值 {self.threshold:.0f}"
