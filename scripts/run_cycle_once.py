"""不启动 HTTP 服务，直接跑一轮完整闭环（收信 → 解析 → 筛选 → 飞书分发）。

适合「8000 窗口被关掉 / 只是想把这一轮跑完」的场景。
所有配置与原服务完全一致（读同一个 .env、同一个 var/hr.db、同一个收信游标），
只是省掉了 uvicorn。

用法：
    python scripts/run_cycle_once.py
退出码：0 = 跑完（errors 为空）；1 = 跑完但有错误。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from core.db import init_models  # noqa: E402
from core.deps import build_container, build_services  # noqa: E402
from core.logging import configure_logging  # noqa: E402


async def _run() -> int:
    """跑一轮闭环并打印结果。"""
    settings = Settings()
    configure_logging()
    container = build_container(settings)
    await init_models(container.engine)

    print("=" * 60)
    print(" 一轮完整闭环（直接调用编排器，不经过 HTTP）")
    print("=" * 60)
    print(f"库：{settings.database_url}")
    print(f"邮件：{settings.mail_mode} / 飞书：{settings.feishu_mode} / LLM：{settings.llm_mode}")
    print()

    async with container.session_factory() as session:
        services = build_services(session, container)
        result = await services.orchestrator.run_daily_cycle()

    print()
    print("-" * 60)
    print(f"cycle_id        : {result.cycle_id}")
    print(f"ingested        : {result.ingested}   （本轮入库简历）")
    print(f"parsed          : {result.parsed}   （成功解析正文）")
    print(f"screened_passed : {result.screened_passed}   （通过并触发飞书）")
    print(f"screened_rejected: {result.screened_rejected}")
    print(f"duplicates      : {result.duplicates}")
    print(f"packages        : {len(result.packages)} 个")
    for pkg in result.packages:
        names = "、".join(pkg.candidate_names or []) or "-"
        print(
            f"  · {pkg.position_title}（{pkg.position_code}） 主管={pkg.supervisor} "
            f"投递={pkg.received_count} 通过={pkg.passed_count}"
        )
        print(f"    名单：{names}")
        print(
            f"    飞书={'成功' if pkg.feishu_message_id else '失败'} "
            f"({pkg.feishu_message_id or '无'})"
        )
        print(
            f"    邮件={'成功' if pkg.email_message_id else '失败'} "
            f"({pkg.email_message_id or '无'})"
        )
        if pkg.error:
            print(f"    错误：{pkg.error}")
    if result.errors:
        print("errors:")
        for err in result.errors:
            print(f"  ! {err}")
    else:
        print("errors          : 无")
    print("-" * 60)
    if result.screened_passed:
        print(
            f"结论：{result.screened_passed} 份通过，飞书已按 {len(result.packages)} 个部门包推送"
            "（每包含文本汇总 + 简历压缩包）。"
        )
    elif result.errors:
        print("结论：本轮有错误，未产生可分发简历（飞书不会发消息）。")
    else:
        print("结论：本轮没有简历通过筛选，按设计不会发飞书。")
    print()
    print("提示：接下来如果要在飞书里回复面试安排 / 确认发送面试通知，")
    print("      需要先双击「启动飞书确认服务.bat」（服务 + 长连接两个窗口都别关），")
    print("      否则你在飞书里的回复没有进程接收。")
    return 0 if not result.errors else 1


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
