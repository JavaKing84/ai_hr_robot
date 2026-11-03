"""应用生命周期：建表、启动 APScheduler（每日闭环 + 入站轮询）、关闭清理。

APScheduler 锁 3.x（``apscheduler>=3.11,<4``）：v4.0 仍是 pre-release，
官方 README 明确不建议生产使用，这里用 ``AsyncIOScheduler`` + ``CronTrigger``。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from core.db import init_models
from core.deps import Container, build_services, get_container
from core.logging import configure_logging, get_logger, log_extra

logger = get_logger(__name__)

DAILY_JOB_ID = "hr_daily_cycle"
INBOUND_JOB_ID = "hr_inbound_poll"


async def run_daily_job(container: Container) -> None:
    async with container.session_factory() as session:
        try:
            services = build_services(session, container)
            await services.orchestrator.run_daily_cycle()
        except Exception:  # noqa: BLE001 - 定时任务必须自己兜住异常，否则调度器会静默丢任务
            await session.rollback()
            logger.exception("job.daily_failed")


async def poll_inbound_job(container: Container) -> None:
    async with container.session_factory() as session:
        try:
            services = build_services(session, container)
            await services.orchestrator.poll_inbound()
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.exception("job.inbound_failed")


def build_scheduler(container: Container) -> AsyncIOScheduler:
    settings = container.settings
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    cron = settings.schedule_cron or container.company_config.schedule.cron or "0 9 * * *"
    scheduler.add_job(
        run_daily_job,
        CronTrigger.from_crontab(cron, timezone=settings.timezone),
        args=[container],
        id=DAILY_JOB_ID,
        name="每日招聘闭环",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    if settings.inbound_poll_seconds > 0:
        scheduler.add_job(
            poll_inbound_job,
            IntervalTrigger(seconds=settings.inbound_poll_seconds, timezone=settings.timezone),
            args=[container],
            id=INBOUND_JOB_ID,
            name="入站邮件轮询",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )

    logger.info(
        "scheduler.configured",
        extra=log_extra(cron=cron, inbound_poll_seconds=settings.inbound_poll_seconds),
    )
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = get_container()
    settings = container.settings
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)
    await init_models(container.engine)

    scheduler: AsyncIOScheduler | None = None
    if settings.scheduler_enabled:
        scheduler = build_scheduler(container)
        scheduler.start()
        container.scheduler = scheduler
        app.state.scheduler = scheduler
        logger.info("app.started", extra=log_extra(jobs=[job.id for job in scheduler.get_jobs()]))
    else:
        logger.info("app.started", extra=log_extra(scheduler="disabled"))

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await container.aclose()
        logger.info("app.stopped")


def scheduler_running(app: FastAPI) -> bool:
    scheduler = getattr(app.state, "scheduler", None)
    return bool(scheduler is not None and scheduler.running)
