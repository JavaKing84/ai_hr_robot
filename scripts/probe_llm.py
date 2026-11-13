"""大模型链路体检：逐家探测 LLM_PROVIDERS，输出「谁可用、谁降级」。

用法：
    .venv\\Scripts\\python.exe scripts\\probe_llm.py

输出四列：序号 / 供应商名 / 结果 / 说明。
顺序即 .env 里的 LLM_PROVIDERS 顺序，也是真实业务里的降级顺序。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.llm import LLMClient  # noqa: E402
from config.settings import Settings  # noqa: E402

_PROMPT = '只输出一个 JSON 对象：{"ok": true, "role": "hr-probe"}'


async def main() -> int:
    settings = Settings()
    client = LLMClient(settings)

    print("=" * 72)
    print(" LLM 链路体检")
    print("=" * 72)
    print(f"LLM_MODE={settings.llm_mode}  enabled={client.enabled}  深度解析={settings.llm_deep_analysis}")
    providers = client._candidate_providers()
    if not providers:
        print("！没有任何供应商配置（LLM_PROVIDERS 与 LLM_MODEL 都为空）")
        return 1

    ok_count = 0
    for index, provider in enumerate(providers, start=1):
        started = time.perf_counter()
        try:
            data = await client._call_litellm(
                provider, system="你是连通性探测助手。", user=_PROMPT, temperature=0.0, max_tokens=200
            )
            cost = (time.perf_counter() - started) * 1000
            ok_count += 1
            print(f"[{index}] {provider['name']:<22} OK    {cost:6.0f} ms  {str(data)[:60]}")
        except Exception as exc:  # noqa: BLE001 - 体检就是要拿到每一家的失败原因
            cost = (time.perf_counter() - started) * 1000
            print(f"[{index}] {provider['name']:<22} FAIL  {cost:6.0f} ms  {type(exc).__name__}: {str(exc)[:90]}")

    # 再跑一次业务入口，验证「失败自动换下一家 + 全挂静默降级」这条真实路径
    started = time.perf_counter()
    result = await client.safe_complete_json(system="你是连通性探测助手。", user=_PROMPT)
    cost = (time.perf_counter() - started) * 1000
    print("-" * 72)
    if result is None:
        print(f"safe_complete_json -> None（{cost:.0f} ms）：全挂时静默降级到规则抽取，业务不报错")
    else:
        print(f"safe_complete_json -> {str(result)[:60]}（{cost:.0f} ms），最终命中：{client.last_provider}")
    print(f"可用供应商：{ok_count}/{len(providers)}")
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
