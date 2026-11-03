"""业务异常：统一携带机器可读 code 与 HTTP 状态码，由中间件转成固定错误信封。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有可预期业务错误的基类。"""

    code: str = "internal_error"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or {}


class ValidationFailed(AppError):
    code = "validation_failed"
    status_code = 400


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class ConflictError(AppError):
    code = "conflict"
    status_code = 409


class ConfigurationError(AppError):
    code = "configuration_error"
    status_code = 500


class ExternalServiceError(AppError):
    code = "external_service_error"
    status_code = 502


class LLMUnavailable(ExternalServiceError):
    code = "llm_unavailable"
    status_code = 503


class MailboxUnavailable(ExternalServiceError):
    code = "mailbox_unavailable"
    status_code = 503
