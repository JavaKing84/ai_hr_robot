"""LLM 客户端：经 litellm 统一网关调用，只负责“把 JSON 拿回来”。

三层降级（每一层都静默，不抛到业务层）：

1. ``LLM_PROVIDERS`` 里的多供应商链，按顺序逐个尝试（学院 qwen3.8-27b →
   学院 qwen3.6-35b-a3b → DeepSeek 兜底）。每个供应商有**自己的 api_base 与
   api_key**——这是 2026-11-13 的关键改动：旧实现把 ``OPENAI_BASE_URL`` 全局
   塞给 litellm，只能服务同一个供应商，跨端点混用必然 401/404。
2. 没配 ``LLM_PROVIDERS`` 时退回单供应商（``LLM_MODEL`` + ``OPENAI_BASE_URL`` +
   ``OPENAI_API_KEY``）+ ``LLM_FALLBACK_MODELS``，兼容旧部署。
3. 全部失败 → 抛 ``LLMUnavailable``，由调用方（extraction / screening）落到
   确定性规则抽取与规则打分。**离线环境永远有结果**，只是没有语义分。

降级只发生在「调用失败 / 超时 / 返回不是合法 JSON」，不会因为“模型返回了但
抽取不理想”而降级（那会绕过姓名接地校验）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from config.settings import Settings
from core.exceptions import ExternalServiceError, LLMUnavailable
from core.logging import get_logger

# litellm 冷启动会去 GitHub 拉「模型价格表」，国内直连必然超时（实测 3 次重试 ~10s），
# 而且它默认把 "LiteLLM completion() model=..." 打到 stdout，25 份简历就刷满屏。
# 这两行必须在 import litellm 之前生效。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")
logging.getLogger("LiteLLM").setLevel(logging.ERROR)

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


def extract_json_objects(text: str) -> list[str]:
    """从一段夹杂散文/思考过程的文本里，按括号配对抽所有 `{...}` 候选。

    学院那台 vLLM 上的 qwen3.6 会把答案埋在 ``reasoning`` 里（"Here's a thinking
    process: ... final JSON {...}"），散文里还有花括号，简单地「第一个 { 到最后一个 }」
    切出来的片段不是合法 JSON。这里用配对计数切出每个顶层对象，调用方从后往前试，
    取第一个能 ``json.loads`` 的——思考型模型的最终答案通常在最后一段。
    """
    candidates: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    candidates.append(text[start : index + 1])
                    start = -1
    return candidates


def _parse_first_json_object(text: str) -> dict[str, Any] | None:
    """从散文里挑出第一个能解析的 JSON 对象（从后往前试，思考型答案在最后）。"""
    for candidate in reversed(extract_json_objects(text)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        #: 运行期健康度：最近一次成功的供应商名，便于日志/接口排查
        self.last_provider: str = ""

    # ------------------------------------------------------------------ 配置
    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    def _candidate_providers(self) -> list[dict[str, str]]:
        """返回尝试顺序：``LLM_PROVIDERS`` 链 → 单供应商配置 → fallback 列表。"""
        chain = list(self.settings.llm_provider_chain)
        if chain:
            return chain

        primary = self.settings.llm_model.strip()
        ordered: list[dict[str, str]] = []
        for model in [primary, *self.settings.llm_fallback_model_list]:
            if not model:
                continue
            if any(item["model"] == model for item in ordered):
                continue
            ordered.append(
                {
                    "name": model,
                    "model": model,
                    "api_base": self.settings.openai_base_url.rstrip("/"),
                    "api_key": self.settings.openai_api_key,
                }
            )
        return ordered

    # ------------------------------------------------------------------ 调用
    async def _call_litellm(
        self,
        provider: dict[str, str],
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        import litellm  # 延迟导入：无 LLM 场景（规则抽取主路径）不加载该依赖

        # litellm 默认往 stdout 打 "LiteLLM completion() model=..."，一轮 25 份简历
        # 会刷满控制台，把业务日志冲没；关掉它的调试输出（失败信息我们自己记 warning）。
        litellm.suppress_debug_info = True
        litellm.set_verbose = False

        response = await litellm.acompletion(
            model=provider["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            api_base=(provider.get("api_base") or "").rstrip("/") or None,
            api_key=provider.get("api_key") or None,
            timeout=self.settings.llm_timeout_seconds,
        )

        try:
            message = response["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError(
                f"LLM 响应结构异常: {response}", code="llm_bad_response"
            ) from exc

        if not content:
            # 学院那台 vLLM 上的 qwen3.6 是「思考型」模型：max_tokens 偏小时会把整段
            # 思考过程放进 reasoning、content 返回 null。这里读 reasoning 兜底，
            # 后面 strip_json_fence 会从中截出 JSON；两条都空才算失败。
            content = (
                message.get("reasoning_content")
                or message.get("reasoning")
                or ""
            )
        if not content:
            raise ExternalServiceError("LLM 返回空内容", code="llm_bad_response")

        try:
            parsed = json.loads(strip_json_fence(str(content)))
        except json.JSONDecodeError as exc:
            parsed = _parse_first_json_object(str(content))
            if parsed is None:
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
            raise LLMUnavailable("LLM 未启用（缺少 key 或 LLM_MODE=off）")

        providers = self._candidate_providers()
        if not providers:
            raise LLMUnavailable("未配置任何 LLM 供应商（LLM_PROVIDERS / LLM_MODEL 均为空）")

        last_error: Exception | None = None
        for provider in providers:
            try:
                result = await self._call_litellm(
                    provider,
                    system=system,
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if self.last_provider != provider["name"]:
                    logger.info(
                        "llm.provider_ok",
                        extra={"provider": provider["name"], "model": provider["model"]},
                    )
                self.last_provider = provider["name"]
                return result
            except Exception as exc:  # noqa: BLE001 - 任何底层异常都视为「这家不可用」
                last_error = exc
                logger.warning(
                    "llm.provider_failed",
                    extra={
                        "provider": provider.get("name"),
                        "model": provider.get("model"),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )

        logger.warning(
            "llm.all_fallback_failed",
            extra={"providers": [p["name"] for p in providers], "error": str(last_error)},
        )
        raise LLMUnavailable(f"LLM 全部供应商调用失败: {last_error}") from last_error

    async def safe_complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> dict[str, Any] | None:
        """静默版调用：任何失败都返回 ``None``，永不抛异常。

        给「增强了更好、没有也能跑」的能力用（深度解析 / 语义分）。调用方拿到
        ``None`` 就走规则兜底，业务不感知、日志里只留一条 warning。
        """
        try:
            return await self.complete_json(
                system=system, user=user, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001 - 静默兜底：这里就是要吃掉所有异常
            logger.warning("llm.safe_call_failed", extra={"error": str(exc)})
            return None

    async def close(self) -> None:
        # litellm 无长连接客户端需要显式关闭；保留此方法以维持接口稳定。
        return None
