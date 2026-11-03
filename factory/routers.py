"""路由装配：只挂载 ``api/v1/router.py``，业务前缀统一在 ``api/v1`` 内声明。"""

from __future__ import annotations

from fastapi import FastAPI

from api.v1.router import api_router


def include_routers(app: FastAPI) -> None:
    app.include_router(api_router, prefix="/api/v1")
