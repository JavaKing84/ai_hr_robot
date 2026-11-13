"""Alembic 迁移环境：接入异步引擎 + ORM 元数据。

关键点：
- 用 ``asyncpg`` 驱动（异步），URL 从 ``config/settings.py`` 的 ``DATABASE_URL`` 注入；
- ``target_metadata`` 指向 ``core.db.Base.metadata``，让 autogenerate 能比对模型差异；
- 导入 ``model`` 包以注册全部 ORM 模型。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 让 `import config` / `import model` / `import core` 可用（仓库根加入 sys.path）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.settings import get_settings  # noqa: E402
from core.db import Base  # noqa: E402
import model  # noqa: E402,F401  注册全部 ORM 模型

config = context.config

# 从 .env / 环境变量注入数据库 URL（异步引擎用）
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url():
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 而不连库。"""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：真实连库执行迁移（异步）。"""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
