"""跨进程互斥：用 PostgreSQL 会话级咨询锁（advisory lock）保证「同一件事只有一个实例在跑」。

为什么需要：``AsyncIOScheduler`` 的 ``max_instances=1`` 只在**单个进程内**生效。
一旦部署两个副本（滚动更新、双机热备、docker compose scale=2），每天 09:00 会有
两个进程同时跑一轮闭环——重复打包、重复发简历包、重复发面试通知。

实现要点：
- ``pg_try_advisory_lock(key)`` 不阻塞，拿不到立刻返回 False，调用方跳过本次执行；
- 锁是**会话级**的，所以必须固定在一条连接上：从 engine 里 check-out 一条连接，
  用完 ``pg_advisory_unlock`` 再归还。用 ``engine.connect()`` 而不是 session，
  避免 session 归还连接池后锁跟着丢；
- 进程被 kill 时连接断开，PG 自动释放锁，不会出现「死锁住再也没人跑」。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.logging import get_logger, log_extra

logger = get_logger(__name__)

#: 固定锁号（任意常量，只要全项目一致）。daily / inbound / migrate 各一个，互不干扰。
LOCK_DAILY_CYCLE = 918_001
LOCK_INBOUND_POLL = 918_002
LOCK_MIGRATION = 918_003
LOCK_FEISHU_POLL = 918_004


@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, key: int, *, name: str = "") -> AsyncIterator[bool]:
    """尝试获取咨询锁；``yield`` 出是否拿到。

    拿不到时业务代码应直接跳过（这是正常的「另一个实例正在跑」，不是错误）::

        async with advisory_lock(engine, LOCK_DAILY_CYCLE, name="daily") as acquired:
            if not acquired:
                return
            await run_daily_cycle()
    """
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
        if not acquired:
            logger.info("lock.busy", extra=log_extra(lock=name or key))
            yield False
            return
        try:
            yield True
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await conn.commit()
            except Exception:  # noqa: BLE001 - 连接已断则锁由 PG 自动释放
                logger.warning("lock.release_failed", extra=log_extra(lock=name or key))


__all__ = [
    "LOCK_DAILY_CYCLE",
    "LOCK_INBOUND_POLL",
    "LOCK_MIGRATION",
    "advisory_lock",
]
