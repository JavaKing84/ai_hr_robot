"""应用工厂：组装 FastAPI 实例、中间件、路由与健康检查。

没有任何前端/静态资源：本产品的界面是飞书对话与邮件。
"""

from __future__ import annotations

from fastapi import FastAPI

from config.settings import Settings, get_settings
from core.deps import Container, build_container, get_container, set_container
from core.logging import configure_logging, get_logger
from core.timeparse import now_local
from factory.lifespan import lifespan, scheduler_running
from factory.middleware import configure_middlewares
from factory.routers import include_routers
from schema.common import HealthResponse

logger = get_logger(__name__)

DESCRIPTION = """
ai-hr｜飞书 HR 招聘机器人（无界面）

每日定时闭环：收简历 → AI 解析筛选 → 飞书打包发主管 → 主管在飞书或原邮件上回复名单与时间
→ 自动给候选人发面试通知邮件 → 监控 HR 邮箱处理改期/放弃回复。

本服务只提供内部运维接口（手动触发、查询留痕），**不面向终端用户提供页面**。
"""


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()
    app_container = set_container(container or build_container(settings))

    app = FastAPI(
        title="ai-hr 飞书 HR 招聘机器人",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.container = app_container

    configure_middlewares(app)
    include_routers(app)

    @app.get("/health", response_model=HealthResponse, tags=["ops"], summary="健康检查")
    async def health() -> HealthResponse:
        current = get_container()
        local_now = now_local(current.settings.timezone)
        return HealthResponse(
            status="ok" if not current.config_error else "degraded",
            app=current.settings.app_name,
            environment=current.settings.environment,
            time=local_now.isoformat(timespec="seconds"),
            scheduler_running=scheduler_running(app),
            components={
                "database": current.settings.database_url.split("://")[0],
                "feishu_mode": current.settings.feishu_mode,
                "mail_mode": current.settings.mail_mode,
                "llm_enabled": current.settings.llm_enabled,
                "company": current.company_config.company.name,
                "positions": len(current.company_config.positions),
                "config_error": current.config_error,
            },
        )

    return app


__all__ = ["create_app"]
