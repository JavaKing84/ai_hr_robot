"""版本无关的逻辑备份：用 asyncpg 的 COPY 文本协议把整库导成可还原的平面文件。

**为什么需要它**：``pg_dump`` 只能备份「不高于自己版本」的库。本机装的是
PostgreSQL 16 客户端，而云端是 18.6，``pg_dump`` 直接报
``server version: 18.6 ... pg_dump version: 16.15`` 并退出 1。这不是权限或网络问题，
换连接参数也解决不了——只能换备份方式。

COPY 文本协议是服务端生成的，客户端版本无关；还原时用同一条协议灌回去，
jsonb / timestamptz 都不会失真（不经过 Python 类型系统，避免时区、转义二次损坏）。

产出目录结构：

```
<out>/manifest.json          # 表清单、列顺序、行数、库版本、备份时间
<out>/<schema>.<table>.copy  # 每张表一份 COPY 文本
```

还原（示例见 ``--restore-hint``）：

```sql
\\copy public.positions (id, code, ...) FROM 'public.positions.copy'
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:  # pragma: no cover
    print("缺少 asyncpg，请在项目虚拟环境里运行：./.venv/Scripts/python.exe scripts/pg_logical_backup.py")
    raise SystemExit(2)


def parse_url(url: str) -> dict[str, object]:
    """把 SQLAlchemy / libpq 连接串拆成 asyncpg 连接参数。"""
    raw = url.strip()
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "postgres://",
        "postgresql://",
    ):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix) :]
            break
    from urllib.parse import unquote, urlparse

    parts = urlparse(raw)
    return {
        "host": parts.hostname or "localhost",
        "port": parts.port or 5432,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "database": (parts.path or "/").lstrip("/"),
    }


async def backup(url: str, out_dir: Path) -> int:
    params = parse_url(url)
    if not params["database"]:
        print("连接串缺少库名", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
    root = out_dir / f"{params['database']}_{stamp}"
    root.mkdir(parents=True, exist_ok=True)

    conn = await asyncpg.connect(**params, timeout=20)
    try:
        version = await conn.fetchval("SHOW server_version")
        tables = await conn.fetch(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name
            """
        )

        manifest: dict[str, object] = {
            "database": params["database"],
            "host": params["host"],
            "port": params["port"],
            "server_version": version,
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "tables": [],
        }

        for row in tables:
            schema, table = row["table_schema"], row["table_name"]
            columns = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                ORDER BY ordinal_position
                """,
                schema,
                table,
            )
            names = [c["column_name"] for c in columns]
            count = await conn.fetchval(f'SELECT count(*) FROM "{schema}"."{table}"')

            dest = root / f"{schema}.{table}.copy"
            # COPY 文本协议由服务端生成：空值写 \N，制表符/换行自动转义，
            # 不经过 Python 的 str/datetime 转换，所以不会被时区或编码弄坏。
            with dest.open("wb") as fh:
                await conn.copy_from_query(
                    f'SELECT * FROM "{schema}"."{table}"', output=fh, format="text"
                )

            manifest["tables"].append(
                {
                    "schema": schema,
                    "table": table,
                    "columns": names,
                    "rows": int(count or 0),
                    "file": dest.name,
                    "bytes": dest.stat().st_size,
                }
            )
            print(f"  {schema}.{table:<22} {count:>8} 行  {dest.stat().st_size:>9} 字节")

        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total = sum(int(t["rows"]) for t in manifest["tables"])  # type: ignore[index]
        print(f"\n备份完成：PostgreSQL {version} · {len(manifest['tables'])} 张表 · {total} 行")
        print(f"目录：{root}")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="版本无关的 PostgreSQL 逻辑备份")
    parser.add_argument("--url", required=True, help="postgresql://user:pass@host:port/db")
    parser.add_argument(
        "--out",
        default="backups",
        help="输出目录（默认项目下 backups/）",
    )
    args = parser.parse_args()
    return asyncio.run(backup(args.url, Path(args.out)))


if __name__ == "__main__":
    raise SystemExit(main())
