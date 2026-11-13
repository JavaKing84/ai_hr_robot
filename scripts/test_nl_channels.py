"""双通道自然语言自测：同一句人话，分别从飞书和邮件灌进来，看机器人听懂没有。

    uv run python scripts/test_nl_channels.py

做的事：
1. 用独立的临时库 + 全 mock 通道，跑一轮闭环造出真实候选人（不碰你的正式库）；
2. 把一批 HR/主管口语指令按 feishu、email 两个通道各走一遍 ``feedback.handle``；
3. 逐条打印「意图 → 动作 → 是否通过」，最后给出通过率。

它验证的是「理解 + 路由」这一层，不验证飞书/邮箱真实投递（那需要真凭证）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._bootstrap import bootstrap  # noqa: E402
from scripts.ingest_sample import SAMPLES, _email_bytes  # noqa: E402

from core.db import init_models  # noqa: E402
from core.deps import build_services  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from repository.position import PositionRepository  # noqa: E402
from schema.message import InboundMessage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from model.base import utcnow_naive  # noqa: E402
from model.candidate import Candidate  # noqa: E402
from model.supervisor import Supervisor  # noqa: E402


@dataclass(slots=True)
class Case:
    """一条测试指令。``expect`` 拿到 (action, reply) 判断是否通过。"""

    text: str
    expect: Callable[[str, str], bool]
    hint: str


def _action_is(*prefixes: str) -> Callable[[str, str], bool]:
    return lambda action, _reply: any(action.startswith(p) for p in prefixes)


def _reply_has(*keywords: str) -> Callable[[str, str], bool]:
    return lambda _action, reply: any(k in reply for k in keywords)


def _inbound(channel: str, sender: str, text: str, subject: str = "") -> InboundMessage:
    return InboundMessage(
        channel=channel,  # type: ignore[arg-type]
        sender_id=sender,
        sender_name="主管",
        text=text,
        thread_id=f"oc_nl_{uuid.uuid4().hex[:6]}" if channel == "feishu" else "",
        message_id=f"om_nl_{uuid.uuid4().hex[:8]}",
        received_at=utcnow_naive(),
        subject=subject or ("回复：候选人安排" if channel == "email" else ""),
    )


async def _prepare(container) -> tuple[str, str, list[str]]:
    """造数据并返回 (主管飞书 open_id, 主管邮箱, 候选人姓名列表)。"""
    settings = container.settings
    async with container.session_factory() as session:
        await PositionRepository(session).sync_from_config(settings.load_company_config())
        await session.commit()

    for sample in SAMPLES:
        (settings.mock_inbox_dir / f"nl-{sample['name']}.eml").write_bytes(_email_bytes(sample))

    async with container.session_factory() as session:
        services = build_services(session, container)
        await services.orchestrator.run_daily_cycle()

    async with container.session_factory() as session:
        rows = list((await session.execute(select(Candidate).order_by(Candidate.score.desc()))).scalars())
        names = [c.name for c in rows if c.name][:6]

    # 主管账号直接读库，避免依赖配置里写死的 open_id
    async with container.session_factory() as session:
        sup = (await session.execute(select(Supervisor).limit(1))).scalar_one_or_none()
    if sup is None:
        raise SystemExit("没有主管记录，先跑 seed")
    return sup.feishu_open_id, sup.email, names


async def _run_cases(container, channel: str, sender: str, names: list[str]) -> list[tuple[Case, str, str, bool]]:
    a = names[0]
    b = names[1] if len(names) > 1 else names[0]
    cases = [
        Case("今天一共收到多少份简历？", _action_is("agent_query_stats"), "统计数量"),
        Case("现在都有哪些候选人？", _action_is("agent_query_candidates"), "候选人名单"),
        Case(f"{a} 为什么分这么高？", _action_is("agent_explain_score"), "分数解释"),
        Case(f"{a} 会 Kubernetes 吗？", _action_is("agent_query_resume"), "简历问答"),
        Case("你能帮我干什么？", _action_is("agent_chat"), "能力介绍"),
        Case("先筛 3 份给我看看", _action_is("screen_top"), "筛选前N份"),
        Case(f"请通知{a}、{b}，下周三下午两点来公司面试", _action_is("interview_scheduled"), "安排面试"),
        Case("确认", _action_is("interview_notice", "approved", "confirm"), "确认放行"),
        Case(f"{b} 不太合适，淘汰吧", _action_is("reject", "candidate_rejected"), "淘汰候选人"),
    ]

    results = []
    for case in cases:
        async with container.session_factory() as session:
            services = build_services(session, container)
            outcome = await services.feedback.handle(_inbound(channel, sender, case.text))
        ok = case.expect(outcome.action, outcome.reply_text or "")
        results.append((case, outcome.action, (outcome.reply_text or "").strip(), ok))
    return results


def _print_results(channel: str, results: list[tuple[Case, str, str, bool]]) -> int:
    label = "飞书" if channel == "feishu" else "邮件"
    print(f"\n{'=' * 78}\n【{label}通道】\n{'=' * 78}")
    passed = 0
    for case, action, reply, ok in results:
        mark = "✓" if ok else "✗"
        passed += ok
        snippet = reply.replace("\n", " ")[:60]
        print(f"{mark} {case.hint:<8} 说「{case.text}」")
        print(f"    → 动作 {action or '(无)'}｜回复 {snippet}")
    print(f"  小计：{passed}/{len(results)} 通过")
    return passed


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=["both", "feishu", "email"], default="both")
    args = parser.parse_args()

    configure_logging(level="WARNING")
    container = bootstrap()
    settings = container.settings
    print(f"数据库：{settings.database_url.rsplit('@', 1)[-1]}")
    print(f"通道：飞书={settings.feishu_mode}｜邮件={settings.mail_mode}｜LLM={'on' if settings.llm_enabled else 'off'}")
    if not settings.llm_enabled:
        print("!! LLM_MODE 未开启，自然语言识别会退化到规则兜底，测出来的结果不代表真实能力")

    await init_models(container.engine)
    open_id, email, names = await _prepare(container)
    print(f"主管：飞书 {open_id}｜邮箱 {email}\n候选人：{'、'.join(names) or '（无）'}")

    total = passed = 0
    channels = ["feishu", "email"] if args.channel == "both" else [args.channel]
    for channel in channels:
        sender = open_id if channel == "feishu" else email
        results = await _run_cases(container, channel, sender, names)
        passed += _print_results(channel, results)
        total += len(results)

    print(f"\n{'=' * 78}\n总计：{passed}/{total} 通过（{passed / total:.0%}）\n{'=' * 78}")
    await container.aclose()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
