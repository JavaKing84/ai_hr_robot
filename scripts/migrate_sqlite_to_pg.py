"""把旧 SQLite 运行库整表迁到 PostgreSQL（本地 `var/hr_robot.db` → `DATABASE_URL`）。

背景：§4-A.6(a) 起本项目「本地开发与调试也统一用 PostgreSQL」。此前用 SQLite 跑出来的
数据（`var/hr_robot.db`）需要一次性搬进 PG，否则历史简历/事件/消息全部丢失。

用法：
    # 1) 先起 PG（二选一）：docker compose up -d postgres  /  连本机已装的 PG
    # 2) 试运行（只统计，不写库）
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_pg.py --dry-run

    # 3) 正式迁移
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_pg.py

    # 4) 目标库已有脏数据、要清空重灌（谨慎：只清这 6 张业务表）
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_pg.py --truncate

约定：
- **源库只读**：以 URI 只读模式打开 SQLite，迁移脚本不会改动旧库一个字节（可反复跑）。
- **保留原始主键**：`id` 原样搬过去，事件/消息里的 `candidate_id` 等引用才不会错位；
  灌完后重置 PG 序列，否则新插入会撞主键。
- **幂等**：目标库已有同 `id` 的行直接跳过；重复执行不会产生副本。
- **类型按 ORM 定义转换**：SQLite 把日期存成字符串、JSON 存成 TEXT、布尔存成 0/1，
  这里按 `Base.metadata` 的列类型逐列转换后再写 PG（避免 asyncpg 类型报错）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._bootstrap import bootstrap  # noqa: E402

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, text  # noqa: E402
from sqlalchemy.sql.schema import Column  # noqa: E402

from config.settings import get_settings  # noqa: E402
from core.db import Base, init_models  # noqa: E402

#: 迁移顺序按「被引用 → 引用方」排，便于人工核对（本项目未建物理外键，顺序不影响正确性）
TABLE_ORDER: tuple[str, ...] = (
    "positions",
    "supervisors",
    "candidates",
    "email_threads",
    "messages",
    "events",
)


def _coerce(column: Column, value: Any) -> Any:
    """把 SQLite 里的值转换成 ORM 列类型能接受的 Python 值。"""
    if value is None:
        return None
    column_type = column.type
    if isinstance(column_type, DateTime):
        if isinstance(value, datetime):
            return value
        # SQLite 里是 "YYYY-MM-DD HH:MM:SS[.ffffff]"（naive UTC，见 I-04）
        return datetime.fromisoformat(str(value).replace("Z", ""))
    if isinstance(column_type, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return bool(value)
    if isinstance(column_type, JSON):
        if isinstance(value, (str, bytes)):
            text_value = value.decode("utf-8") if isinstance(value, bytes) else value
            return json.loads(text_value) if text_value.strip() else None
        return value
    if isinstance(column_type, Integer):
        return int(value)
    if isinstance(column_type, Float):
        return float(value)
    return value


def read_sqlite_rows(sqlite_path: Path, table: str) -> list[dict[str, Any]]:
    """只读打开旧库，返回整表数据（表不存在则返回空列表，兼容早期库）。"""
    # mode=ro：只读打开，杜绝误写旧库
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return []
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
    finally:
        connection.close()


async def migrate(sqlite_path: Path, truncate: bool, dry_run: bool) -> int:
    settings = get_settings()
    container = bootstrap(settings)
    engine = container.engine
    target = settings.database_url
    print(f"源库（只读）: {sqlite_path}")
    print(f"目标库      : {target}")
    if not sqlite_path.exists():
        print(f"✗ 找不到旧库 {sqlite_path}，没有可迁移的数据。")
        return 1
    if target.startswith("sqlite"):
        print("✗ 目标库仍是 SQLite；请先把 .env 的 DATABASE_URL 设为 PostgreSQL 再跑本脚本。")
        return 1

    if not dry_run:
        await init_models(engine)

    total_rows = 0
    for table_name in TABLE_ORDER:
        table = Base.metadata.tables.get(table_name)
        if table is None:  # pragma: no cover - 元数据缺表说明模型被改坏了
            print(f"! 跳过未知表 {table_name}")
            continue
        rows = read_sqlite_rows(sqlite_path, table_name)
        if not rows:
            print(f"- {table_name:14s} 源库 0 行")
            continue
        if dry_run:
            print(f"- {table_name:14s} 源库 {len(rows)} 行（dry-run，不写入）")
            total_rows += len(rows)
            continue

        async with engine.begin() as connection:
            if truncate:
                await connection.execute(text(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE'))
            existing: set[int] = set()
            if not truncate:
                result = await connection.execute(text(f'SELECT id FROM "{table_name}"'))
                existing = {row[0] for row in result}

            inserted = 0
            skipped = 0
            for row in rows:
                if row.get("id") in existing:
                    skipped += 1
                    continue
                values = {
                    column.name: _coerce(column, row.get(column.name))
                    for column in table.columns
                    if column.name in row
                }
                await connection.execute(table.insert().values(**values))
                inserted += 1
            total_rows += inserted
            print(f"- {table_name:14s} 源库 {len(rows)} 行 → 写入 {inserted}，跳过已存在 {skipped}")

    if not dry_run:
        await _reset_sequences(engine)
        print(f"✓ 迁移完成，共写入 {total_rows} 行；PG 自增序列已重置到当前最大 id。")
    else:
        print(f"✓ dry-run 结束，共 {total_rows} 行待迁移。")
    await engine.dispose()
    return 0


async def _reset_sequences(engine) -> None:
    """保留原始 id 后必须重置序列，否则下一次 INSERT 会从 1 开始撞主键。"""
    async with engine.begin() as connection:
        for table_name in TABLE_ORDER:
            if Base.metadata.tables.get(table_name) is None:
                continue
            await connection.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM \"{table_name}\"), 1), true)"
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧 SQLite 库迁移到 PostgreSQL")
    parser.add_argument(
        "--sqlite",
        default=str(Path("var") / "hr_robot.db"),
        help="旧 SQLite 文件路径（默认 var/hr_robot.db）",
    )
    parser.add_argument("--truncate", action="store_true", help="迁移前清空目标库同名表（谨慎）")
    parser.add_argument("--dry-run", action="store_true", help="只统计行数，不写目标库")
    args = parser.parse_args()

    return asyncio.run(
        migrate(Path(args.sqlite).resolve(), truncate=args.truncate, dry_run=args.dry_run)
    )


if __name__ == "__main__":
    raise SystemExit(main())
