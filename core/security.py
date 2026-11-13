"""内部接口鉴权。

本服务的 HTTP 接口是**运维接口**（手动触发轮询、导入简历、查留痕），不是给终端用户
的产品页面；但在此之前它们完全不设防——任何能连上 8000 端口的人都能触发
「给主管发简历包 / 给候选人发面试邮件」。这里补一层最小可用的令牌鉴权：

- ``API_TOKEN`` 配置后：``/api/v1/*`` 全部要求 ``X-API-Token: <token>``
  或 ``Authorization: Bearer <token>``，两种写法都认（前者方便 curl，后者方便网关）；
- ``API_TOKEN`` 留空：不拦截，但启动时打一条 ``security.api_token_missing`` 警告，
  并在 ``/health`` 里把 ``auth`` 显示为 ``disabled``——「没配」这件事必须看得见，
  而不是悄悄无鉴权。

``/health`` 与飞书回调不在此列：前者被容器健康检查调用（且不含敏感信息），
后者由飞书平台的 token / 签名校验保护（见 ``client/feishu.py``）。
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status

from config.settings import Settings
from core.deps import get_settings_dep

#: 请求头名（两种都支持）
TOKEN_HEADER = "X-API-Token"
AUTH_HEADER = "Authorization"


def _extract_token(x_api_token: str | None, authorization: str | None) -> str:
    if x_api_token:
        return x_api_token.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return ""


async def require_api_token(
    x_api_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    authorization: str | None = Header(default=None, alias=AUTH_HEADER),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """FastAPI 依赖：校验内部接口令牌。未配置 ``API_TOKEN`` 时放行（开发模式）。"""
    expected = (settings.api_token or "").strip()
    if not expected:
        return
    provided = _extract_token(x_api_token, authorization)
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或错误的 API 令牌（请在请求头带 X-API-Token）",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = ["AUTH_HEADER", "TOKEN_HEADER", "require_api_token"]
