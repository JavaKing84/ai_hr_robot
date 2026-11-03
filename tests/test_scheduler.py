"""调度测试：APScheduler 3.x 任务注册、cron 解析、任务入口可被触发。"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from factory.lifespan import (
    DAILY_JOB_ID,
    INBOUND_JOB_ID,
    build_scheduler,
    poll_inbound_job,
    run_daily_job,
)


def test_scheduler_registers_daily_cron_job(container) -> None:
    scheduler = build_scheduler(container)
    job = scheduler.get_job(DAILY_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "9"
    assert fields["minute"] == "0"


def test_inbound_poll_job_disabled_when_seconds_zero(container) -> None:
    scheduler = build_scheduler(container)
    assert scheduler.get_job(INBOUND_JOB_ID) is None


def test_inbound_poll_job_registered_when_enabled(container) -> None:
    container.settings.inbound_poll_seconds = 120
    scheduler = build_scheduler(container)
    job = scheduler.get_job(INBOUND_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)


def test_cron_comes_from_settings_override(container) -> None:
    container.settings.schedule_cron = "30 18 * * *"
    scheduler = build_scheduler(container)
    job = scheduler.get_job(DAILY_JOB_ID)
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "18"
    assert fields["minute"] == "30"


async def test_run_daily_job_invokes_orchestrator(container, monkeypatch) -> None:
    called: dict[str, bool] = {}

    class StubOrchestrator:
        async def run_daily_cycle(self) -> None:
            called["daily"] = True

    class StubServices:
        orchestrator = StubOrchestrator()

    monkeypatch.setattr("factory.lifespan.build_services", lambda session, cont: StubServices())
    await run_daily_job(container)
    assert called.get("daily") is True


async def test_poll_inbound_job_swallows_errors(container, monkeypatch) -> None:
    class BrokenServices:
        class orchestrator:  # noqa: N801
            @staticmethod
            async def poll_inbound() -> None:
                raise RuntimeError("imap down")

    monkeypatch.setattr("factory.lifespan.build_services", lambda session, cont: BrokenServices())
    # 不抛异常即视为通过：定时任务必须自己兜住错误
    await poll_inbound_job(container)
