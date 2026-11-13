"""依赖装配（应用容器 + 请求级服务包）。

- ``Container``：进程级单例，持有设置、引擎、外部客户端、业务配置；
- ``build_services``：请求/任务级，绑定一个 AsyncSession，构造各用例服务；
- ``get_db`` / ``get_services``：FastAPI 依赖注入入口。

服务之间只通过构造参数协作，没有全局可变状态，测试可以直接用内存 SQLite 构造一套。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from client.feishu import FeishuClient
from client.llm import LLMClient
from client.mailbox import MailboxClient
from client.resume_parser import ResumeParser
from config.settings import Settings, get_settings
from core.db import build_engine, build_session_factory
from core.exceptions import ConfigurationError
from core.logging import get_logger, log_extra
from core.timeparse import now_utc_naive
from schema.config import CompanyConfig
from service.email_threading import EmailThreadService
from service.extraction import ExtractionService
from service.feedback import FeedbackService
from service.inbound_email import InboundEmailService
from service.ingestion import IngestionService
from service.notification import NotificationService
from service.orchestrator import Orchestrator
from service.screening import ScreeningService

logger = get_logger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    feishu: FeishuClient
    llm: LLMClient
    mailbox: MailboxClient
    parser: ResumeParser
    extraction: ExtractionService
    company_config: CompanyConfig
    started_at: datetime = field(default_factory=now_utc_naive)
    scheduler: Any = None
    config_error: str = ""

    async def aclose(self) -> None:
        await self.llm.close()
        await self.engine.dispose()


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    settings.ensure_dirs()

    company_config = CompanyConfig()
    config_error = ""
    try:
        company_config = settings.load_company_config()
    except Exception as exc:  # noqa: BLE001 - 配置写坏时不要拖垮整个进程
        config_error = f"{type(exc).__name__}: {exc}"
        logger.error("config.load_failed", extra=log_extra(error=config_error))

    engine = build_engine(settings.database_url, echo=settings.db_echo)
    llm = LLMClient(settings)
    parser = ResumeParser(settings)

    return Container(
        settings=settings,
        engine=engine,
        session_factory=build_session_factory(engine),
        feishu=FeishuClient(settings),
        llm=llm,
        mailbox=MailboxClient(settings),
        parser=parser,
        extraction=ExtractionService(settings, llm=llm, parser=parser),
        company_config=company_config,
        config_error=config_error,
    )


_container: Container | None = None


def set_container(container: Container) -> Container:
    global _container
    _container = container
    return container


def get_container() -> Container:
    if _container is None:
        raise ConfigurationError("应用容器尚未初始化")
    return _container


@dataclass
class Services:
    """一个数据库会话内的一组用例服务。"""

    session: AsyncSession
    container: Container
    extraction: ExtractionService
    screening: ScreeningService
    ingestion: IngestionService
    notification: NotificationService
    threads: EmailThreadService
    feedback: FeedbackService
    inbound: InboundEmailService
    orchestrator: Orchestrator


def build_services(session: AsyncSession, container: Container | None = None) -> Services:
    container = container or get_container()
    settings = container.settings
    company = container.company_config
    llm = container.llm

    extraction = ExtractionService(settings, llm=llm, parser=container.parser)
    screening = ScreeningService(
        session, llm=llm, company_name=company.company.name, settings=settings
    )
    notification = NotificationService(
        session,
        settings=settings,
        feishu=container.feishu,
        mailbox=container.mailbox,
        company=company.company,
        mailbox_config=company.mailbox,
    )
    ingestion = IngestionService(
        session,
        settings=settings,
        parser=container.parser,
        extraction=extraction,
        llm=llm,
        company_name=company.company.name,
        feishu=container.feishu,
        hr_alert_open_id=_first_supervisor_open_id(company),
    )
    threads = EmailThreadService(session, settings)
    feedback = FeedbackService(
        session,
        settings=settings,
        extraction=extraction,
        notification=notification,
        company=company.company,
        mailbox_config=company.mailbox,
        llm=llm,
    )
    inbound = InboundEmailService(
        session,
        settings=settings,
        mailbox=container.mailbox,
        threads=threads,
        feedback=feedback,
        ingestion=ingestion,
        notification=notification,
        extraction=extraction,
        mailbox_config=company.mailbox,
    )
    orchestrator = Orchestrator(
        session,
        settings=settings,
        company_config=company,
        ingestion=ingestion,
        notification=notification,
        inbound=inbound,
        container=container,
    )
    return Services(
        session=session,
        container=container,
        extraction=extraction,
        screening=screening,
        ingestion=ingestion,
        notification=notification,
        threads=threads,
        feedback=feedback,
        inbound=inbound,
        orchestrator=orchestrator,
    )


# ------------------------------------------------------------------ 配置派生


def _first_supervisor_open_id(company: CompanyConfig) -> str:
    """取第一个有效主管飞书 open_id 作为「未归位简历」告警接收人。

    复用 ``config/positions.yaml`` 既有的主管映射，不新增任何配置项（§4-A 总纲 P1）。
    没有配置主管 open_id 时返回空串，由 IngestionService 降级为日志告警。
    """
    for department in company.departments:
        open_id = (department.supervisor.feishu_open_id or "").strip()
        if open_id:
            return open_id
    return ""


# ------------------------------------------------------------------ FastAPI 依赖


async def get_db() -> AsyncIterator[AsyncSession]:
    container = get_container()
    async with container.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_services() -> AsyncIterator[Services]:
    container = get_container()
    async with container.session_factory() as session:
        try:
            yield build_services(session, container)
        except Exception:
            await session.rollback()
            raise


def get_settings_dep() -> Settings:
    return get_container().settings


__all__ = [
    "Container",
    "Services",
    "build_container",
    "build_services",
    "get_container",
    "get_db",
    "get_services",
    "get_settings_dep",
    "set_container",
]
