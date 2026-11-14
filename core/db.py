"""数据库引擎 / 会话 / 声明式基类，以及测试用的建库/清库工具。

除「建引擎、建会话、基类」之外只包含三个与数据库生命周期直接相关的小工具：
``derive_test_database_url`` / ``ensure_database_exists`` / ``truncate_all_tables``。
它们让「跑测试」这件事不需要人工建库、人工改配置。
"""

from __future__ import annotations

import socket

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """建异步引擎。只认 PostgreSQL——开发、测试、生产同一套方言，不留第二套分支。"""
    if not database_url.startswith("postgresql"):
        raise ValueError(
            "DATABASE_URL 必须是 PostgreSQL（postgresql+asyncpg://...），"
            f"收到：{database_url.split('://')[0]}"
        )
    return create_async_engine(
        database_url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=20
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def init_models(engine: AsyncEngine) -> None:
    """建表（幂等）。

    运行时用 Alembic 迁移；测试与一次性脚本用本函数按 ORM 模型直接建表，
    保证「模型即真相」，不依赖迁移文件是否跟得上。
    """
    import model  # noqa: F401  确保全部 ORM 模型完成注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --------------------------------------------------------------- 测试库生命周期

#: 允许被「清空 / 重建」的一次性库后缀。生产库永远不该落在这两种后缀上。
#: 这是最后一道护栏：无论配置怎么写错，只要库名不以此结尾就直接拒绝执行。
DISPOSABLE_SUFFIXES = ("_test", "_e2e")

#: 本机 PostgreSQL 的固定账号（docker-compose.yml 里的 postgres 服务）。
DEFAULT_LOCAL_PG_DSN = "postgresql+asyncpg://hr:hr@localhost:5432"

#: 视为「本机」的主机名。
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def assert_disposable_database(database_url: str, *, purpose: str = "测试") -> str:
    """护栏：确认目标库是一次性库（名字以 ``_test`` / ``_e2e`` 结尾）。

    调用方是「会 ``TRUNCATE`` / ``DROP`` 的代码」——测试夹具、验收脚本。
    它们一旦指错库就会清掉真实数据，所以这里不做「尽量兼容」，而是直接抛错。
    返回库名，方便调用方打印。
    """
    name = make_url(database_url).database or ""
    if not name:
        raise RuntimeError(f"拒绝执行{purpose}：连接串里没有库名（{database_url}）")
    if not name.endswith(DISPOSABLE_SUFFIXES):
        raise RuntimeError(
            f"拒绝在库 {name!r} 上执行{purpose}：库名必须以 "
            f"{' 或 '.join(DISPOSABLE_SUFFIXES)} 结尾。\n"
            "这道护栏用来防止测试 / 验收脚本清空生产库；"
            "需要新的一次性库时，请用带 _test / _e2e 后缀的名字。"
        )
    return name


def is_local_host(host: str | None) -> bool:
    """判断主机名是否指向本机。"""
    return (host or "").lower() in LOCAL_HOSTS


def port_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """探测某个 TCP 端口是否可连（不建连接池、不留连接）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def prefer_local_test_target(
    database_url: str,
    *,
    local_dsn: str = DEFAULT_LOCAL_PG_DSN,
    local_port: int = 5432,
) -> tuple[str, str]:
    """测试目标库优先落在本机：本机 PG 可用时，把远程 URL 换成同机的同名库。

    为什么默认回本机：测试会对目标库执行 ``TRUNCATE``。跑在共享服务器上等于
    每隔几秒清一次别人的库，而且每次用例都要走内网往返。本机能用就绝不用远程。

    本机不可用（没装 / 没启动 Docker）时不报错，保留原远程地址并返回原因，
    由调用方打印醒目提示——断网环境下仍然能跑测试，只是会写共享服务器。

    返回 ``(最终url, 原因)``，原因用于日志与告警。
    """
    url = make_url(database_url)
    if is_local_host(url.host):
        return database_url, "目标已是本机"

    if not port_open("127.0.0.1", local_port):
        return database_url, f"本机 127.0.0.1:{local_port} 未监听，保留远程"

    local_url = make_url(local_dsn).set(database=url.database)
    return (
        local_url.render_as_string(hide_password=False),
        f"本机 127.0.0.1:{local_port} 可用，已切换为同机库",
    )


def derive_test_database_url(database_url: str, *, suffix: str = "_test") -> str:
    """由运行库 URL 派生测试库 URL：``hr_robot`` → ``hr_robot_test``。

    已经是 ``*_test`` 的原样返回，重复调用不会叠后缀（避免 ``hr_robot_test_test``）。
    """
    url = make_url(database_url)
    name = url.database or "hr_robot"
    if not name.endswith(suffix):
        name = f"{name}{suffix}"
    return url.set(database=name).render_as_string(hide_password=False)


async def ensure_database_exists(
    database_url: str,
    *,
    maintenance_db: str = "postgres",
    timeout: float = 10.0,
) -> bool:
    """确保 ``database_url`` 指向的库存在，不存在就自动创建。

    只支持 PostgreSQL。返回 ``True`` 表示本次新建。

    连的是 ``maintenance_db``（默认 ``postgres``）而不是目标库——目标库还不存在时
    必须先连一个已存在的库才能执行 ``CREATE DATABASE``。
    """
    url: URL = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError(f"ensure_database_exists 只支持 PostgreSQL，收到：{url.drivername}")
    target = url.database
    if not target:
        raise ValueError("DATABASE_URL 里没有库名")

    import asyncpg

    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host or "localhost",
        port=url.port or 5432,
        database=maintenance_db,
        timeout=timeout,
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target)
        if exists:
            return False
        # 库名来自本地配置，不含用户输入；仍用双引号包裹以支持大小写/横线
        await conn.execute(f'CREATE DATABASE "{target}"')
        return True
    finally:
        await conn.close()


async def truncate_all_tables(
    engine: AsyncEngine,
    *,
    keep: tuple[str, ...] = ("alembic_version",),
) -> list[str]:
    """清空当前 schema 下所有表（``RESTART IDENTITY CASCADE``），返回被清空的表名。

    测试隔离用：每个用例开始前调用一次，等价于「每个用例一个全新的库」，
    比逐用例建库/建 schema 快得多，也不会残留自增 ID 影响断言。
    ``alembic_version`` 默认保留，避免把迁移版本号一起清掉。
    """
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema() ORDER BY tablename"
            )
        )
        tables = [name for name in rows.scalars().all() if name not in keep]
        if not tables:
            return []
        quoted = ", ".join(f'"{name}"' for name in tables)
        await conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        return tables


__all__ = [
    "Base",
    "DISPOSABLE_SUFFIXES",
    "assert_disposable_database",
    "build_engine",
    "build_session_factory",
    "derive_test_database_url",
    "ensure_database_exists",
    "init_models",
    "is_local_host",
    "port_open",
    "prefer_local_test_target",
    "truncate_all_tables",
]
