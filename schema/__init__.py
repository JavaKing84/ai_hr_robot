"""Pydantic v2 schema 导出。"""

from schema.candidate import (
    CandidateListResponse,
    CandidateRead,
    EmailPollResponse,
    EmailTestRequest,
    EmailTestResponse,
    EventRead,
    FeishuWebhookResponse,
    IngestResponse,
    PackageDispatchResult,
    PositionRead,
    RunCycleResponse,
    SimulateInboundEmailRequest,
)
from schema.common import ErrorBody, ErrorEnvelope, HealthResponse, PageMeta
from schema.config import (
    CompanyConfig,
    HardCondition,
    PositionConfig,
    ScreeningConfig,
    WeightedCriterion,
)
from schema.extraction import (
    CandidateReplySchema,
    ExtractedCandidate,
    InterviewScheduleSchema,
    SourceQuote,
)
from schema.message import Attachment, InboundMessage
from schema.screening import CriterionHit, HardConditionResult, ScreeningResult

__all__ = [
    "Attachment",
    "CandidateListResponse",
    "CandidateRead",
    "CandidateReplySchema",
    "CompanyConfig",
    "CriterionHit",
    "EmailPollResponse",
    "EmailTestRequest",
    "EmailTestResponse",
    "ErrorBody",
    "ErrorEnvelope",
    "EventRead",
    "ExtractedCandidate",
    "FeishuWebhookResponse",
    "HardCondition",
    "HardConditionResult",
    "HealthResponse",
    "InboundMessage",
    "IngestResponse",
    "InterviewScheduleSchema",
    "PackageDispatchResult",
    "PageMeta",
    "PositionConfig",
    "PositionRead",
    "RunCycleResponse",
    "ScreeningConfig",
    "ScreeningResult",
    "SimulateInboundEmailRequest",
    "SourceQuote",
    "WeightedCriterion",
]
