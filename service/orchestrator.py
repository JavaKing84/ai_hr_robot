"""每日闭环编排：收简历 → 解析筛选 → 飞书发主管 → 等反馈 → 邮件通知 → 处理回复。

产品文档第 3 节的执行顺序就是本文件 ``run_daily_cycle`` 的顺序；
任何一步失败都不阻断后续可完成的部分（单职位失败只影响该职位）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from core.logging import get_logger, log_extra
from core.timeparse import now_local, now_utc_naive
from model.candidate import CandidateStatus
from model.event import Actor
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from repository.position import PositionRepository
from schema.candidate import EmailPollResponse, RunCycleResponse
from schema.config import CompanyConfig
from service.inbound_email import InboundEmailService
from service.ingestion import IngestionService
from service.notification import NotificationService

logger = get_logger(__name__)


class Orchestrator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        company_config: CompanyConfig,
        ingestion: IngestionService,
        notification: NotificationService,
        inbound: InboundEmailService,
        container: Any = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.company_config = company_config
        self.ingestion = ingestion
        self.notification = notification
        self.inbound = inbound
        self.container = container
        self.candidates = CandidateRepository(session)
        self.positions = PositionRepository(session)
        self.events = EventRepository(session)

    def _new_cycle_id(self) -> int:
        return int(now_local(self.settings.timezone).strftime("%Y%m%d%H%M"))

    async def sync_config(self) -> dict[str, int]:
        """从磁盘重新读取 YAML 并同步进数据库：HR 改完配置，本轮立即生效（无需重启）。"""
        fresh = self.settings.load_company_config()
        self.company_config = fresh
        if self.container is not None:
            self.container.company_config = fresh
        # 本轮已构造的协作对象也换成新配置（公司信息、面试地点等）
        if self.notification is not None:
            self.notification.company = fresh.company
            self.notification.mailbox_config = fresh.mailbox

        stats = await self.positions.sync_from_config(fresh)
        await self.session.commit()
        logger.info("config.synced", extra=log_extra(**stats))
        return stats

    async def run_daily_cycle(self) -> RunCycleResponse:
        """一轮完整闭环（定时任务入口）。"""
        cycle_id = self._new_cycle_id()
        triggered_at = now_utc_naive()
        errors: list[str] = []

        await self.events.record(
            "cycle.started",
            actor=Actor.SYSTEM,
            object_type="cycle",
            object_id=cycle_id,
            detail={"cron": self.settings.schedule_cron or self.company_config.schedule.cron},
        )

        try:
            await self.sync_config()
        except Exception as exc:  # noqa: BLE001 - 配置同步失败时用库里已有职位继续
            logger.exception("cycle.sync_failed")
            errors.append(f"配置同步失败：{exc}")

        # 1) 收简历 + 处理已到达的邮件回复（同一个轮询，避免互相吞邮件）
        try:
            poll = await self.inbound.poll_once(cycle_id=cycle_id)
            errors.extend(poll.errors)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cycle.poll_failed")
            poll = EmailPollResponse(errors=[str(exc)])
            errors.append(f"邮箱轮询失败：{exc}")

        candidates = await self.candidates.list_by_cycle(cycle_id)
        passed = self._count_status(candidates, CandidateStatus.SCREENED_PASSED)
        rejected = self._count_status(candidates, CandidateStatus.SCREENED_REJECTED)
        duplicates = sum(
            int(detail.get("duplicates", 0) or 0)
            for detail in (poll.details or [])
            if detail.get("kind") == "resume"
        )

        # 2) 按职位打包发给主管（飞书 + 邮件副本）
        packages = []
        try:
            packages = await self.notification.dispatch_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cycle.dispatch_failed")
            errors.append(f"分发给主管失败：{exc}")

        response = RunCycleResponse(
            cycle_id=cycle_id,
            triggered_at=triggered_at,
            ingested=len(candidates),
            parsed=len([c for c in candidates if c.status != CandidateStatus.RECEIVED]),
            screened_passed=passed,
            screened_rejected=rejected,
            duplicates=duplicates,
            packages=packages,
            errors=errors,
        )

        await self.events.record(
            "cycle.completed",
            actor=Actor.SYSTEM,
            object_type="cycle",
            object_id=cycle_id,
            detail=response.model_dump(mode="json"),
            note="本轮任务完成：简历已筛选并分发给主管，等待主管反馈后发面试邮件",
        )
        await self.session.commit()
        logger.info("cycle.completed", extra=log_extra(**response.model_dump(mode="json")))
        return response

    async def poll_inbound(self) -> EmailPollResponse:
        """高频入站轮询（主管反馈 / 候选人回信）。"""
        response = await self.inbound.poll_once()
        logger.info(
            "inbound.polled",
            extra=log_extra(processed=response.processed, ignored=response.ignored, failed=response.failed),
        )
        return response

    @staticmethod
    def _count_status(candidates, status: str) -> int:
        return len([c for c in candidates if c.status == status])


def cycle_timestamp(value: datetime | None = None) -> int:
    return int((value or datetime.now()).strftime("%Y%m%d%H%M"))


__all__ = ["Orchestrator", "cycle_timestamp"]
