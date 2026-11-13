"""v1 路由聚合。"""

from fastapi import APIRouter

from api.v1 import email, feishu, ops, recruitment

api_router = APIRouter()
api_router.include_router(recruitment.router)
api_router.include_router(feishu.router)
api_router.include_router(email.router)
api_router.include_router(ops.router)

__all__ = ["api_router"]
