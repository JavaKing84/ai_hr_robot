"""中间件装配。

顺序说明：Starlette 后添加的中间件在更外层，因此先加访问日志、再加请求 ID，
保证访问日志里能读到 request_id。
"""

from __future__ import annotations

from fastapi import FastAPI

from middlewares.access_log import AccessLogMiddleware
from middlewares.exception import register_exception_handlers
from middlewares.request_id import RequestIdMiddleware


def configure_middlewares(app: FastAPI) -> None:
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
