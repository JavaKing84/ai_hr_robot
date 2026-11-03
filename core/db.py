"""数据库引擎 / 会话 / 声明式基类。除这三件事之外不写任何逻辑。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    kwargs: dict = {"echo": echo, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        # SQLite 单文件、多连接场景（测试 / 本地演示）下必须放开线程检查
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return create_async_engine(database_url, **kwargs)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def init_models(engine: AsyncEngine) -> None:
    """建表（MVP 阶段用 create_all；生产可替换为 Alembic 迁移）。"""
    import model  # noqa: F401  确保全部 ORM 模型完成注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
