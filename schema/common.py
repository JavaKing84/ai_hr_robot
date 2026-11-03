"""通用 schema：健康检查、统一错误信封、分页。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str
    environment: str
    time: str
    scheduler_running: bool = False
    components: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int
