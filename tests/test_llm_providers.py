"""LLM 多供应商链与静默降级（2026-11-13 新增能力）。

覆盖四件事：
1. ``LLM_PROVIDERS`` 的解析（补 openai/ 前缀、裁掉 /chat/completions、去重）；
2. 第一家失败自动换下一家，全部失败抛 ``LLMUnavailable``（由业务降级到规则）；
3. ``safe_complete_json`` 全挂时返回 ``None`` 而不是抛异常；
4. 思考型模型把答案放在 ``reasoning`` 里时仍能取到 JSON。
"""

from __future__ import annotations

import json

import pytest

from client.llm import LLMClient, extract_json_objects
from config.settings import Settings
from core.exceptions import LLMUnavailable


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        llm_mode="live",
        llm_providers="",
        llm_model="deepseek/deepseek-chat",
        openai_base_url="https://api.deepseek.com/v1",
        openai_api_key="sk-test",
    )
    base.update(kwargs)
    return Settings(**base)


def test_provider_chain_parsing() -> None:
    raw = json.dumps(
        [
            {"name": "学院A", "model": "qwen3.8-27b", "api_base": "http://a:1/v1/chat/completions", "api_key": "k1"},
            {"name": "学院B", "model": "openai/x", "api_base": "http://b:2/v1", "api_key": "k2"},
            {"name": "重复", "model": "qwen3.8-27b", "api_base": "http://a:1/v1", "api_key": "k1"},
        ]
    )
    settings = _settings(llm_providers=raw)
    chain = settings.llm_provider_chain

    assert [item["name"] for item in chain] == ["学院A", "学院B"]
    # 完整 /chat/completions 地址被裁成 base，模型名自动补 openai/ 前缀
    assert chain[0]["model"] == "openai/qwen3.8-27b"
    assert chain[0]["api_base"] == "http://a:1/v1"
    # 已带前缀的模型不被二次包装
    assert chain[1]["model"] == "openai/x"
    assert settings.llm_enabled is True


def test_bad_providers_json_does_not_raise() -> None:
    settings = _settings(llm_providers="{这不是 JSON")
    assert settings.llm_provider_chain == []


@pytest.mark.asyncio
async def test_fallback_to_next_provider(monkeypatch) -> None:
    settings = _settings(
        llm_providers=json.dumps(
            [
                {"name": "first", "model": "openai/a", "api_base": "http://a/v1", "api_key": "1"},
                {"name": "second", "model": "openai/b", "api_base": "http://b/v1", "api_key": "2"},
            ]
        )
    )
    client = LLMClient(settings)
    calls: list[str] = []

    async def fake_call(provider, *, system, user, temperature, max_tokens):
        calls.append(provider["name"])
        if provider["name"] == "first":
            raise RuntimeError("第一家超时")
        return {"semantic_score": 88}

    monkeypatch.setattr(client, "_call_litellm", fake_call)
    result = await client.complete_json(system="s", user="u")

    assert result == {"semantic_score": 88}
    assert calls == ["first", "second"]
    assert client.last_provider == "second"


@pytest.mark.asyncio
async def test_all_providers_failed_raises_and_safe_returns_none(monkeypatch) -> None:
    settings = _settings(
        llm_providers=json.dumps([{"name": "only", "model": "openai/a", "api_base": "http://a/v1", "api_key": "1"}])
    )
    client = LLMClient(settings)

    async def always_fail(*args, **kwargs):
        raise RuntimeError("全挂")

    monkeypatch.setattr(client, "_call_litellm", always_fail)
    with pytest.raises(LLMUnavailable):
        await client.complete_json(system="s", user="u")
    # 静默兜底：业务侧拿到 None，继续走规则抽取，不抛异常
    assert await client.safe_complete_json(system="s", user="u") is None


def test_extract_json_from_thinking_reasoning() -> None:
    text = (
        "Here's a thinking process:\n1. 先看 {需求}，再算 {分数}\n"
        '最终答案：{"semantic_score": 72, "reason": "技术栈对口"}'
    )
    objects = extract_json_objects(text)
    assert objects[-1] == '{"semantic_score": 72, "reason": "技术栈对口"}'
