"""拉一次飞书群消息并处理——把「等主管回话」变成一条可以手动触发的命令。

为什么单独成脚本：常驻服务的定时拉取适合生产，但人工演练时需要「我回一句、你立刻
响应」的确定性节奏。本脚本复用同一条解析管道（``FeedbackService.handle``），
行为与线上完全一致，只是触发方式从定时器换成手动。

用法::

    # 只看最近 10 分钟的新消息，处理并打印结果
    python scripts/feishu_tick.py

    # 只看不发（不写入数据库、不往群里回话），用于确认读到了什么
    python scripts/feishu_tick.py --dry-run

    # 指定起点（秒级时间戳），跳过历史消息
    python scripts/feishu_tick.py --since 1794574300
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# 直接 `python scripts/feishu_tick.py` 时 sys.path[0] 是 scripts/，项目根不在里面，
# 补一下才能 import core / service。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deps import build_container, build_services, set_container  # noqa: E402


async def tick(since: int, dry_run: bool, limit: int) -> int:
    container = set_container(build_container())
    settings = container.settings
    chat_id = (settings.feishu_chat_id or "").strip()
    if not chat_id:
        print("未配置 FEISHU_CHAT_ID，无法拉取群消息")
        return 2

    try:
        items = await container.feishu.list_chat_messages(
            chat_id, start_time=since or None, page_size=limit
        )
        if not items:
            print("群里没有新消息（或应用没有 im:message:readonly 权限）")
            return 0

        print(f"拉到 {len(items)} 条新消息：\n")
        async with container.session_factory() as session:
            services = build_services(session, container)
            for item in items:
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                sender = item.get("sender_id") or ""
                print(f"  [{item['received_at']:%H:%M:%S}] {sender[-8:]}：{text}")
                if dry_run:
                    continue
                if await services.messages.seen_inbound("feishu", item["message_id"]):
                    print("      ↳ 已处理过，跳过")
                    continue
                inbound = container.feishu.to_inbound_message(item)
                try:
                    outcome = await services.feedback.handle(inbound)
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    print(f"      ↳ 处理异常：{type(exc).__name__}: {exc}")
                    continue
                print(
                    f"      ↳ action={outcome.action} handled={outcome.handled} "
                    f"detail={outcome.detail}"
                )
                if outcome.reply_text:
                    print(f"      ↳ 已回话：{outcome.reply_text[:160]}")
        return 0
    finally:
        await container.engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="拉一次飞书群消息并处理")
    parser.add_argument(
        "--since", type=int, default=0, help="起始时间戳（秒）；默认最近 10 分钟"
    )
    parser.add_argument("--limit", type=int, default=20, help="最多拉多少条")
    parser.add_argument("--dry-run", action="store_true", help="只读，不处理不回话")
    args = parser.parse_args()
    since = args.since or int(time.time()) - 600
    return asyncio.run(tick(since, args.dry_run, args.limit))


if __name__ == "__main__":
    sys.exit(main())
