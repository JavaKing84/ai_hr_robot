"""把本轮筛出的人按部门打包派发给对应主管（演练用触发器）。

与线上派发同一段代码（``NotificationService.dispatch_cycle``），只是触发方式
从定时任务换成手动。邮件走 ``MAIL_MODE=mock``（只落 .eml 不发），飞书走 live。

用法::

    python scripts/dispatch_cycle.py            # 默认本轮批次
    python scripts/dispatch_cycle.py --cycle-id 202611140520
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deps import build_container, build_services, set_container  # noqa: E402

DEFAULT_CYCLE = 202611140520


async def main() -> int:
    parser = argparse.ArgumentParser("dispatch_cycle")
    parser.add_argument("--cycle-id", type=int, default=DEFAULT_CYCLE)
    args = parser.parse_args()

    container = set_container(build_container())
    print(f"cycle={args.cycle_id}  mail_mode={container.settings.mail_mode}  "
          f"feishu_mode={container.settings.feishu_mode}\n")

    async with container.session_factory() as session:
        services = build_services(session, container)
        results = await services.notification.dispatch_cycle(args.cycle_id)
        if not results:
            print("没有可派发的候选人（该批次无 screened_passed）")
        for r in results:
            data = r.model_dump() if hasattr(r, "model_dump") else r.__dict__
            print(
                f"[{'OK' if data.get('ok') else 'FAIL'}] "
                f"{data.get('position_code')} / {data.get('position_title')} → "
                f"{data.get('supervisor')} | 收到 {data.get('received_count')} 通过 "
                f"{data.get('passed_count')} | 包={data.get('package_name') or '-'} | "
                f"err={data.get('error') or '-'}"
            )
        await session.commit()

    await container.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
