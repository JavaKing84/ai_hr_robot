"""初始化数据库并写入测试“公司”：3 个部门 + 3 位主管 + 在招职位与筛选规则。

用法：
    python scripts/seed.py            # 建表 + 同步 config/positions.yaml
    python scripts/seed.py --reset    # 先清空本项目的业务表（谨慎：只清 hr_robot 库）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许 `python scripts/seed.py` 直接运行（把仓库根加入 import 路径）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._bootstrap import bootstrap  # noqa: E402

from core.db import Base, init_models
from core.logging import configure_logging, get_logger, log_extra
from repository.position import PositionRepository

logger = get_logger("scripts.seed")


async def reset_tables(container) -> None:
    """清空并重建本项目的业务表（demo 与 seed --reset 共用）。"""
    async with container.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_models(container.engine)


async def seed(reset: bool = False) -> None:
    container = bootstrap()
    settings = container.settings
    await init_models(container.engine)

    if reset:
        await reset_tables(container)
        print("已清空并重建数据表")

    config = settings.load_company_config()
    async with container.session_factory() as session:
        repo = PositionRepository(session)
        stats = await repo.sync_from_config(config)
        await session.commit()
        positions = await repo.list_active()

        print(f"公司：{config.company.name}")
        for department in config.departments:
            supervisor = department.supervisor
            print(f"  部门 {department.name}｜主管 {supervisor.name}（{supervisor.title}）｜飞书 {supervisor.feishu_open_id}")
            for position in department.positions:
                print(f"    - {position.code} {position.title}｜阈值 {position.screening.threshold}")
        print(f"同步结果：{stats}")
        print(f"当前在招职位：{len(positions)} 个")
        logger.info("seed.done", extra=log_extra(positions=len(positions), **stats))

    await container.aclose()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="初始化 ai-hr 数据库与测试公司")
    parser.add_argument("--reset", action="store_true", help="先删除并重建所有表")
    args = parser.parse_args()
    try:
        asyncio.run(seed(reset=args.reset))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
