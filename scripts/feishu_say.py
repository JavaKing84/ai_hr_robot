"""往飞书群说一句话——演练时「机器人主动提问」的入口。

为什么单独成脚本：常驻服务只在业务动作里往群里发消息，而演练需要我随时
以 HR / 机器人身份主动开口（例如「本轮招什么岗、要几份简历」）。本脚本复用
同一个 ``FeishuClient.send_group_text``，走应用身份，@ 谁由
``FEISHU_MENTION_OPEN_IDS`` 决定，与线上通道完全一致。

用法::

    python scripts/feishu_say.py "文本"
    python scripts/feishu_say.py --no-mention "文本"
    python scripts/feishu_say.py --at-all "文本"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.feishu import FeishuClient  # noqa: E402
from config.settings import get_settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser("feishu_say")
    parser.add_argument("text", help="要发到群里的话")
    parser.add_argument("--at-all", action="store_true", help="@所有人")
    parser.add_argument("--no-mention", action="store_true", help="不 @ 任何人")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.feishu_chat_id:
        print("FEISHU_CHAT_ID 未配置，无法发群消息", file=sys.stderr)
        return 2
    if args.no_mention:
        # 临时清空 mention 列表，复用同一条发送通道。
        settings.feishu_mention_open_ids = ""

    client = FeishuClient(settings)
    result = await client.send_group_text(args.text, at_all=args.at_all)
    print(
        f"ok={result.ok} mode={result.mode} "
        f"message_id={getattr(result, 'message_id', '')} error={getattr(result, 'error', '')}"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
