"""只建表，不做任何数据写入（容器首次启动前的兜底）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._bootstrap import bootstrap  # noqa: E402

from core.db import init_models
from core.logging import configure_logging


async def main() -> None:
    container = bootstrap()
    await init_models(container.engine)
    print(f"数据表已就绪：{container.settings.database_url}")
    await container.aclose()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
