"""应用生命周期：迁移 schema、启动 APScheduler（每日闭环 + 入站轮询）、关闭清理。

关于建表（2026-11-14 收敛双轨）：以前是 ``create_all`` 与 Alembic 并行——模型改了、
迁移没写，线上照样能起，于是「线上表结构和仓库里不一致」这件事只能靠人记。
现在统一走 Alembic：启动时 ``upgrade head``（``AUTO_MIGRATE=false`` 可关掉），
迁移本身也用咨询锁串行化，多个副本同时启动不会互相打架。

APScheduler 锁 3.x（``apscheduler>=3.11,<4``）：v4.0 仍是 pre-release，
官方 README 明确不建议生产使用，这里用 ``AsyncIOScheduler`` + ``CronTrigger``。
单进程内的 ``max_instances=1`` 管不住多副本，所以两个任务都套一层 PG 咨询锁。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from sqlalchemy import text

from config.settings import BASE_DIR
from core.deps import Container, build_services, get_container
from core.locks import (
    LOCK_DAILY_CYCLE,
    LOCK_FEISHU_POLL,
    LOCK_INBOUND_POLL,
    LOCK_MIGRATION,
    advisory_lock,
)
from core.logging import configure_logging, get_logger, log_extra

logger = get_logger(__name__)

DAILY_JOB_ID = "hr_daily_cycle"
INBOUND_JOB_ID = "hr_inbound_poll"
FEISHU_JOB_ID = "hr_feishu_poll"


async def run_daily_job(container: Container) -> None:
    # 多副本部署时只有一个实例能拿到锁，其余实例这一分钟直接跳过（不报错）
    async with advisory_lock(container.engine, LOCK_DAILY_CYCLE, name=DAILY_JOB_ID) as acquired:
        if not acquired:
            return
        async with container.session_factory() as session:
            try:
                services = build_services(session, container)
                await services.orchestrator.run_daily_cycle()
            except Exception:  # noqa: BLE001 - 定时任务必须自己兜住异常，否则调度器会静默丢任务
                await session.rollback()
                logger.exception("job.daily_failed")


async def poll_inbound_job(container: Container) -> None:
    async with advisory_lock(container.engine, LOCK_INBOUND_POLL, name=INBOUND_JOB_ID) as acquired:
        if not acquired:
            return
        async with container.session_factory() as session:
            try:
                services = build_services(session, container)
                await services.orchestrator.poll_inbound()
            except Exception:  # noqa: BLE001
                await session.rollback()
                logger.exception("job.inbound_failed")


async def poll_feishu_job(container: Container) -> None:
    """主动拉群消息，喂给与 webhook 完全相同的解析管道。

    存在的理由：webhook 要求飞书服务器能回连到本机。开发机、内网部署、没做穿透
    的环境全都收不到 ``im.message.receive_v1``——机器人把包发到群里看着正常，主管
    回话却石沉大海。拉取不依赖入站网络，只要机器人在群里、应用有 ``im:message:readonly``
    就能读回来。两条通道同时开着也不冲突：``messages.external_id`` 的幂等唯一索引
    会挡掉同一条消息的第二次处理。
    """
    async with advisory_lock(container.engine, LOCK_FEISHU_POLL, name=FEISHU_JOB_ID) as acquired:
        if not acquired:
            return
        chat_id = (container.settings.feishu_chat_id or "").strip()
        if not chat_id:
            return
        async with container.session_factory() as session:
            services = build_services(session, container)
            try:
                items = await container.feishu.list_chat_messages(chat_id, page_size=20)
            except Exception:  # noqa: BLE001
                logger.exception("job.feishu_poll_failed")
                return

            handled = 0
            for item in items:
                message_id = item.get("message_id") or ""
                if not message_id:
                    continue
                if container.settings.feishu_require_mention and not item.get("mentions"):
                    continue
                if not (item.get("text") or "").strip():
                    continue
                if await services.messages.seen_inbound("feishu", message_id):
                    continue
                inbound = container.feishu.to_inbound_message(item)
                try:
                    outcome = await services.feedback.handle(inbound)
                    handled += 1
                    logger.info(
                        "feishu.poll_handled",
                        extra=log_extra(
                            message_id=message_id,
                            action=outcome.action,
                            handled=outcome.handled,
                        ),
                    )
                except Exception:  # noqa: BLE001 - 单条失败不拖累整轮
                    await session.rollback()
                    logger.exception("feishu.poll_handle_failed", extra=log_extra(message_id=message_id))
            if items:
                logger.info(
                    "feishu.poll_done",
                    extra=log_extra(seen=len(items), handled=handled),
                )


# ------------------------------------------------------------------ schema 迁移


def _upgrade_schema_sync(database_url: str) -> None:
    """同步执行 ``alembic upgrade head``（在独立线程里跑，避免阻塞事件循环）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    # 显式注入目标库：绝对不能依赖 .env，否则测试/多库环境会迁移错库
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


async def _current_revision(container: Container) -> str:
    try:
        async with container.engine.connect() as conn:
            value = await conn.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return str(value or "")
    except Exception:  # noqa: BLE001 - 表不存在即「未迁移」
        return ""


async def ensure_schema(container: Container) -> str:
    """确保 schema 是最新的，返回当前 revision（失败时返回空串）。"""
    settings = container.settings
    if not settings.auto_migrate:
        revision = await _current_revision(container)
        logger.info("schema.migrate_skipped", extra=log_extra(revision=revision or "unknown"))
        return revision

    try:
        async with advisory_lock(container.engine, LOCK_MIGRATION, name="migrate") as acquired:
            if acquired:
                await asyncio.to_thread(_upgrade_schema_sync, settings.database_url)
    except Exception:  # noqa: BLE001 - 迁移失败不要让容器起不来，/health 会显示 unmigrated
        logger.exception("schema.migrate_failed")
        return ""

    revision = await _current_revision(container)
    logger.info("schema.ready", extra=log_extra(revision=revision or "unknown"))
    return revision


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

    if settings.feishu_poll_seconds > 0:
        scheduler.add_job(
            poll_feishu_job,
            IntervalTrigger(seconds=settings.feishu_poll_seconds, timezone=settings.timezone),
            args=[container],
            id=FEISHU_JOB_ID,
            name="飞书群消息拉取",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )

    logger.info(
        "scheduler.configured",
        extra=log_extra(
            cron=cron,
            inbound_poll_seconds=settings.inbound_poll_seconds,
            feishu_poll_seconds=settings.feishu_poll_seconds,
        ),
    )
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = get_container()
    settings = container.settings
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)

    schema_revision = await ensure_schema(container)
    app.state.schema_revision = schema_revision

    if not (settings.api_token or "").strip():
        logger.warning(
            "security.api_token_missing",
            extra=log_extra(
                hint="内部接口当前无鉴权，仅适合本机开发；部署前请在 .env 配 API_TOKEN"
            ),
        )

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
