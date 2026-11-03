"""LLM 客户端：OpenAI 兼容 ``/chat/completions``，只负责“把 JSON 拿回来”。

- 不绑定任何厂商 SDK；``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``LLM_MODEL`` 全部来自环境变量。
- ``llm_mode=auto`` 且没有 key 时抛 ``LLMUnavailable``，调用方降级到确定性规则抽取，
  这样离线 / 无凭证环境下整条闭环依然可以跑通（见 DECISIONS.md）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from config.settings import Settings
from core.exceptions import ExternalServiceError, LLMUnavailable
from core.logging import get_logger

logger = get_logger(__name__)

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def strip_json_fence(content: str) -> str:
    """去掉模型偶尔加上的 ```json 围栏。"""
    cleaned = content.strip()
    cleaned = _JSON_FENCE_RE.sub("", cleaned).strip()
    if cleaned.startswith("{") or cleaned.startswith("["):
        return cleaned
    # 兜底：截取第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


class LLMClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.openai_base_url.rstrip("/"),
                timeout=self.settings.llm_timeout_seconds,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            )
        return self._client

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> dict[str, Any]:
        """调用模型并解析 JSON。失败抛 ``LLMUnavailable`` / ``ExternalServiceError``。"""
        if not self.enabled:
            raise LLMUnavailable("LLM 未启用（缺少 OPENAI_API_KEY 或 LLM_MODE=off）")

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        try:
            response = await self._http().post("/chat/completions", json=payload)
            if response.status_code == 400:
                # 部分兼容实现不支持 response_format，去掉后重试一次
                payload.pop("response_format", None)
                response = await self._http().post("/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ExternalServiceError(
                f"LLM 返回错误状态 {exc.response.status_code}: {exc.response.text[:300]}",
                code="llm_http_error",
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"LLM 调用失败: {exc}", code="llm_network_error") from exc

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(f"LLM 响应结构异常: {data}", code="llm_bad_response") from exc

        try:
            parsed = json.loads(strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise ExternalServiceError(
                f"LLM 返回内容不是合法 JSON: {content[:300]}", code="llm_bad_json"
            ) from exc
        if not isinstance(parsed, dict):
            raise ExternalServiceError("LLM 返回的 JSON 顶层必须是对象", code="llm_bad_json")
        return parsed

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
