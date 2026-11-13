"""统一异常处理：所有错误都返回 ``{"error": {"code", "message", "request_id"}}``。

业务异常（``AppError``）保留自己的 code / status；未预期异常统一 500 并记录堆栈，
不把内部细节泄给调用方。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import AppError
from core.logging import get_logger, log_extra, request_id_ctx

logger = get_logger(__name__)


def error_payload(code: str, message: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_ctx.get(),
            "details": details or {},
        }
    }


#: 常见 HTTP 状态对应的可读 code。没有这张表时 401 会变成 ``http_401``，
#: 调用方（尤其是监控脚本）只能靠解析字符串判断，既难读也容易写错。
_HTTP_CODE_NAMES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error",
            extra=log_extra(code=exc.code, path=request.url.path, message=exc.message),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload("validation_error", "请求参数不合法", {"errors": exc.errors()}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODE_NAMES.get(exc.status_code, f"http_{exc.status_code}")
        # 401 必须带 WWW-Authenticate（RFC 7235）；原来的实现把 headers 丢了，
        # 浏览器/网关拿不到「该用哪种认证」，只会渲染成一个裸 401。
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra=log_extra(path=request.url.path))
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "服务内部错误，请凭 request_id 联系维护者"),
        )
