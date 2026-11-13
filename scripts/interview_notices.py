"""面试通知确认台：列出挂起的面试通知，确认后再真正发送。

默认只读，不加 --yes 绝不会发信。

用法：
    python scripts/interview_notices.py list                 # 看有哪些待确认
    python scripts/interview_notices.py send --id 3 --id 5 --yes   # 发指定的
    python scripts/interview_notices.py send --all --yes      # 发全部待确认

退出码：0 = 正常；1 = 有错误或未加 --yes。
"""

from __future__ import annotations

import argparse
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

LINE = "─" * 64


async def _list() -> list:
    """打印待确认草稿，返回原始行。"""
    settings = Settings()
    configure_logging()
    container = build_container(settings)
    await init_models(container.engine)

    async with container.session_factory() as session:
        services = build_services(session, container)
        rows = await services.notification.pending_interview_notices()
        print(LINE)
        print(f" 邮件模式：{settings.mail_mode}   确认闸门："
              f"{'开启（必须人工确认）' if settings.interview_notice_require_confirm else '关闭（会直接发送）'}")
        print(LINE)
        if not rows:
            print("当前没有待确认的面试通知。")
            return []
        print(f"共 {len(rows)} 封待确认：\n")
        for row in rows:
            preview = (row.body or "").strip().splitlines()
            head = " / ".join(line.strip() for line in preview[:3] if line.strip())
            print(f"[{row.id}] 收件人 {row.recipient}")
            print(f"     主题 {row.subject}")
            print(f"     开头 {head[:90]}")
            print()
        return rows


async def _send(message_ids: list[int] | None, confirmed: bool) -> int:
    """发送待确认的面试通知；未确认时只回显将要发送的目标。"""
    settings = Settings()
    configure_logging()
    container = build_container(settings)
    await init_models(container.engine)

    async with container.session_factory() as session:
        services = build_services(session, container)
        rows = await services.notification.pending_interview_notices()
        if message_ids:
            wanted = set(message_ids)
            rows = [row for row in rows if row.id in wanted]
        if not rows:
            print("没有匹配的待确认面试通知，未做任何发送。")
            return 0

        print(f"待发送 {len(rows)} 封：")
        for row in rows:
            print(f"  [{row.id}] → {row.recipient}  |  {row.subject}")
        if not confirmed:
            print()
            print("未加 --yes，本次只做预览，没有发送任何邮件。")
            print("确认无误后执行：python scripts/interview_notices.py send --all --yes")
            return 1

        results = await services.notification.confirm_pending_interview_notices(
            [row.id for row in rows]
        )
        ok = len([r for r in results if r.ok])
        print()
        print(f"已发送 {ok}/{len(results)} 封。")
        for result in results:
            print(f"  message_id={result.message_id} detail={result.detail}")
        return 0 if ok == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="面试通知确认台")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出待确认的面试通知")
    send = sub.add_parser("send", help="发送待确认的面试通知")
    send.add_argument("--id", type=int, action="append", default=None, help="指定消息 id，可重复")
    send.add_argument("--all", action="store_true", help="发送全部待确认项")
    send.add_argument("--yes", action="store_true", help="确认执行（不加只预览）")
    args = parser.parse_args()

    if args.cmd == "list":
        asyncio.run(_list())
        return 0
    if not args.all and not args.id:
        print("请指定 --all 或 --id；不做任何发送。")
        return 1
    return asyncio.run(_send(args.id, args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
