"""Config schema: 部门 → 职位 → 主管映射，以及筛选规则。

结构对应产品文档第 6 节 `config.positions[]` / `config.screening_rules[]`，
落盘文件为 ``config/positions.yaml``，由 HR 直接编辑，下一轮任务即生效。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: 硬性/加权条件的比较算子。HR 在 YAML 里写这些算子，代码不再做字符串魔法。
ConditionOperator = Literal[
    "eq",
    "ne",
    "gte",
    "lte",
    "in",
    "not_in",
    "contains_any",
    "contains_all",
    "exists",
]


class SupervisorConfig(BaseModel):
    """部门主管：简历包与对话都在飞书，邮箱用于接收简历包副本（支持“原邮件回复”）."""

    name: str
    title: str = Field(description="称呼，如「张总」，用于飞书消息开头")
    feishu_open_id: str = Field(description="飞书 open_id 或邮箱，机器人据此定向发送")
    email: str = ""


class HardCondition(BaseModel):
    """硬性条件：不过即淘汰（产品文档第 4 节）."""

    field: str = Field(description="候选人字段名，如 education_level / school / years_experience")
    operator: ConditionOperator
    value: Any = None
    label: str = ""

    @model_validator(mode="after")
    def _check_value(self) -> "HardCondition":
        if self.operator in {"gte", "lte", "eq", "ne", "in", "not_in"} and self.value is None:
            raise ValueError(f"硬性条件 {self.field} 的算子 {self.operator} 需要 value")
        return self


class WeightedCriterion(BaseModel):
    """加权打分项：用于排序与取舍，命中得满分，未命中得 0（支持部分命中比例）."""

    field: str
    weight: float = Field(gt=0, description="权重，最终分数按权重归一化到 0-100")
    operator: ConditionOperator = "contains_any"
    value: Any = None
    label: str = ""

    @field_validator("weight")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("weight 必须大于 0")
        return v


class ScreeningConfig(BaseModel):
    """单个职位的筛选配置：硬性条件 + 加权项 + 阈值."""

    threshold: float = Field(default=60, ge=0, le=100, description="加权得分及格线")
    hard_conditions: list[HardCondition] = Field(default_factory=list)
    weighted: list[WeightedCriterion] = Field(default_factory=list)
    semantic_weight: float = Field(
        default=0,
        ge=0,
        le=100,
        description="LLM 语义匹配分在总分中的权重（0 表示只按规则打分）",
    )
    always_pass_names: list[str] = Field(
        default_factory=list,
        description="白名单：名单中的候选人跳过全部筛选直接通过（用于主测试简历验收）",
    )
    rule_version: str = "v1"


class PositionConfig(BaseModel):
    code: str
    title: str
    department: str = ""
    headcount: int = 1
    description: str = Field(default="", description="岗位 JD 摘要，供 LLM 语义匹配使用")
    keywords: list[str] = Field(
        default_factory=list,
        description="职位识别关键词：简历未指定岗位时，用这些词把简历归位到本职位",
    )
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)


class DepartmentConfig(BaseModel):
    name: str
    supervisor: SupervisorConfig
    positions: list[PositionConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_department(self) -> "DepartmentConfig":
        for position in self.positions:
            if not position.department:
                position.department = self.name
        return self


class ScheduleConfig(BaseModel):
    cron: str = Field(default="0 9 * * *", description="每日触发时间，标准 5 段 cron")
    timezone: str = "Asia/Shanghai"
    lookback_hours: int = Field(default=24, ge=1, description="收简历时间窗（小时）")
    max_resumes_per_cycle: int = Field(default=500, ge=1)


class MailboxConfig(BaseModel):
    address: str = "hr@example.com"
    sender_name: str = "星野科技 HR"
    interview_location: str = Field(default="", description="默认面试地点；为空时邮件写“地点待定”")
    meeting_link: str = ""
    reply_token_domain: str = Field(
        default="",
        description="reply+<token>@<domain> 兜底路由域名；空则用 HR 邮箱域名",
    )


class CompanyInfo(BaseModel):
    name: str = "示例科技有限公司"
    hr_name: str = "李静"
    hr_title: str = "HR 招聘专员"
    hr_contact: str = ""


class CompanyConfig(BaseModel):
    """`config/positions.yaml` 的完整结构."""

    company: CompanyInfo = Field(default_factory=CompanyInfo)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    mailbox: MailboxConfig = Field(default_factory=MailboxConfig)
    departments: list[DepartmentConfig] = Field(default_factory=list)

    @property
    def positions(self) -> list[PositionConfig]:
        return [p for d in self.departments for p in d.positions]

    def position_by_code(self, code: str) -> PositionConfig | None:
        for position in self.positions:
            if position.code == code:
                return position
        return None

    def department_of(self, position_code: str) -> DepartmentConfig | None:
        for department in self.departments:
            if any(p.code == position_code for p in department.positions):
                return department
        return None

    def supervisor_of(self, position_code: str) -> SupervisorConfig | None:
        department = self.department_of(position_code)
        return department.supervisor if department else None
