"""LLM 客户端：经 litellm 统一网关调用，只负责“把 JSON 拿回来”。

- 多供应商统一入口由 ``litellm.acompletion`` 承担，模型名即供应商前缀（``gpt-4o-mini`` /
  ``claude-3-5-sonnet`` / ``gemini/...`` 等），``OPENAI_API_KEY`` 等凭证由 litellm 按供应商路由。
- 失败自动降级顺序：主模型 ``LLM_MODEL`` → 依序尝试 ``LLM_FALLBACK_MODELS`` 逗号分隔列表 →
  全部失败抛 ``LLMUnavailable``，由调用方（screening / extraction）落到确定性规则抽取。
- **降级只发生在「调用失败 / 超时 / 无 key」**，绝不因“LLM 返回了结果但抽取不理想”而降级
  （那会绕过 I-05 姓名接地）。
- ``llm_mode=off`` 或无 key 时抛 ``LLMUnavailable``，调用方降级到确定性规则抽取，
  这样离线 / 无凭证环境下整条闭环依然可以跑通（见 DECISIONS.md）。
"""

from __future__ import annotations

import json
import re
from typing import Any

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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    def _candidate_models(self) -> list[str]:
        """返回尝试顺序：主模型 → fallback 列表（已去空去重、并排除主模型自身）。"""
        primary = self.settings.llm_model.strip()
        fallbacks = [m for m in self.settings.llm_fallback_model_list if m != primary]
        ordered = [primary] if primary else []
        ordered.extend(fallbacks)
        # 去重保序
        seen: set[str] = set()
        result: list[str] = []
        for m in ordered:
            if m not in seen:
                seen.add(m)
                result.append(m)
        return result

    async def _call_litellm(self, model: str, *, system: str, user: str, temperature: float, max_tokens: int) -> dict[str, Any]:
        import litellm  # 延迟导入：无 LLM 场景（规则抽取主路径）不加载该依赖

        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            api_base=self.settings.openai_base_url.rstrip("/"),
            api_key=self.settings.openai_api_key or None,
            timeout=self.settings.llm_timeout_seconds,
        )

        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(
                f"LLM 响应结构异常: {response}", code="llm_bad_response"
            ) from exc

        if content is None:
            raise ExternalServiceError("LLM 返回空内容", code="llm_bad_response")

        try:
            parsed = json.loads(strip_json_fence(str(content)))
        except json.JSONDecodeError as exc:
            raise ExternalServiceError(
                f"LLM 返回内容不是合法 JSON: {str(content)[:300]}", code="llm_bad_json"
            ) from exc
        if not isinstance(parsed, dict):
            raise ExternalServiceError("LLM 返回的 JSON 顶层必须是对象", code="llm_bad_json")
        return parsed

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

        models = self._candidate_models()
        if not models:
            raise LLMUnavailable("未配置任何 LLM 模型（LLM_MODEL 为空）")

        last_error: Exception | None = None
        for model in models:
            try:
                return await self._call_litellm(
                    model,
                    system=system,
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except (ExternalServiceError, LLMUnavailable) as exc:
                # 结构化错误（响应结构 / 非法 JSON）属于“模型返回了但不可用”，
                # 主模型命中的结构错误同样允许尝试下一个模型（仍属调用失败范畴，
                # 不涉及“抽取不理想就降级”的语义判断）。
                last_error = exc
                logger.warning(
                    "llm.call_failed",
                    extra={"model": model, "error": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001 - litellm 抛出的底层异常统一视为调用失败
                last_error = exc
                logger.warning(
                    "llm.call_failed",
                    extra={"model": model, "error": f"{type(exc).__name__}: {exc}"},
                )

        # 全部失败：收敛为 LLMUnavailable，交由调用方落到确定性规则抽取
        logger.warning("llm.all_fallback_failed", extra={"models": models, "error": str(last_error)})
        raise LLMUnavailable(f"LLM 主模型与全部 fallback 均调用失败: {last_error}") from last_error

    async def close(self) -> None:
        # litellm 无长连接客户端需要显式关闭；保留此方法以维持接口稳定。
        return None
