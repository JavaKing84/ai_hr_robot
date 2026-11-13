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
            logger.exception("cycle.sync_failed", extra=log_extra(cycle_id=cycle_id))
            errors.append(f"配置同步失败：{exc}")

        # 1) 收简历 + 处理已到达的邮件回复（同一个轮询，避免互相吞邮件）
        try:
            poll = await self.inbound.poll_once(cycle_id=cycle_id)
            errors.extend(poll.errors)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cycle.poll_failed", extra=log_extra(cycle_id=cycle_id))
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
            logger.exception("cycle.dispatch_failed", extra=log_extra(cycle_id=cycle_id))
            errors.append(f"分发给主管失败：{exc}")

        # 2.5) 给 HR 报一句今天的量，HR 才知道要不要说「筛选 N 份」
        if self._hr_open_id:
            await self._notify_hr_summary(
                ingested=len(candidates), passed=passed, rejected=rejected
            )

        # 3) 幂等补发上一轮失败的出站消息（§4-A.1b：按幂等键查重后安全补发，不产生重复消息）
        try:
            notice_results = await self.notification.retry_failed_interview_notices()
            feishu_resent = await self.notification.retry_failed_feishu_messages()
            logger.info(
                "cycle.notice_retry",
                extra=log_extra(
                    cycle_id=cycle_id,
                    email_retried=len(notice_results),
                    email_ok=len([r for r in notice_results if r.ok]),
                    feishu_resent=feishu_resent,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 补发失败不影响本轮主流程
            logger.exception("cycle.notice_retry_failed", extra=log_extra(cycle_id=cycle_id))
            errors.append(f"补发失败消息异常：{exc}")

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
        elapsed_ms = int((now_utc_naive() - triggered_at).total_seconds() * 1000)
        payload = response.model_dump(mode="json")
        payload["elapsed_ms"] = elapsed_ms
        logger.info("cycle.completed", extra=log_extra(**payload))
        return response

    @property
    def _hr_open_id(self) -> str:
        return (self.settings.hr_feishu_open_id or "").strip()

    async def _notify_hr_summary(self, *, ingested: int, passed: int, rejected: int) -> None:
        """每天一句：收到多少、通过多少、还差哪一步。HR 看到数字才好下「筛选 N 份」这种指令。

        2026-11-14 增加「待确认 / 失败」两个数：这两个数不为 0 说明流程卡在人工环节
        或者投递出了问题，比「今天收到几份」更值得 HR 先看。
        """
        if ingested == 0:
            text = "今天还没有新简历。"
        else:
            text = (
                f"今天收到 {ingested} 份简历，通过 {passed} 份、未通过 {rejected} 份。"
                f"回复「筛选5份」我先给你挑出来。"
            )
        try:
            pending = len(await self.notification.pending_interview_notices())
            failed = len(await self.notification.messages.list_failed_outbound(channel="email"))
            if pending or failed:
                text += f"\n待你确认的面试通知 {pending} 份；发送失败待补发 {failed} 封。"
            await self.notification.notify_supervisor(self._hr_open_id, text, kind="hr_daily_summary")
        except Exception as exc:  # noqa: BLE001 - 报数失败不影响闭环
            logger.warning("cycle.hr_summary_failed", extra=log_extra(error=str(exc)))

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
