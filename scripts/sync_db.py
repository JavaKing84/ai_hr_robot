"""把数据库结构（迁移 + 索引）同步到另一套库（本机 ↔ 云），并当场自证。

为什么要有这个脚本
------------------
``alembic upgrade head`` 只管「结构到哪一版」，不管「到底改对了没有」。
换一台机器、换一个库时真正会出的事故是：迁移跑完了、版本号也对，但
JSON 列还是 ``json``、时间列还是 ``timestamp``、唯一索引根本没建——
因为旧库里已经有数据，某一步悄悄跳过了。

所以这个脚本做三件事，缺一不可：

1. **先备份**（有数据就必须有退路，没有 ``pg_dump`` 就拒绝继续）；
2. **再迁移**（``alembic upgrade head``，URL 显式注入，绝不依赖 ``.env``）；
3. **最后自证**（当场查 ``information_schema`` / ``pg_indexes``，逐条核对
   jsonb、timestamptz、唯一索引、版本号；对不上就以非 0 退出）。

用法
----
本机 → 云：:

    python scripts/sync_db.py --target postgresql://admin:PASS@10.191.44.66:15432/hr_ai_robot

云端还是空库、要把本机数据也带过去：:

    python scripts/sync_db.py --target postgresql://admin:PASS@10.191.44.66:15432/hr_ai_robot \\
        --copy-data --yes

只体检不动手（给同事交接时先看一眼现状）::

    python scripts/sync_db.py --target postgresql://... --check

退出码：0 全部通过 / 1 有检查项不通过 / 2 参数或环境有问题（含拒绝备份）。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import asyncpg

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# 表按外键依赖顺序排列：先父后子，灌数据时才不会被外键拦住。
BUSINESS_TABLES = (
    "positions",
    "supervisors",
    "candidates",
    "email_threads",
    "messages",
    "events",
)

# 0003_hardening 之后必须成立的索引（缺一个就说明迁移没真正落地）
REQUIRED_INDEXES = (
    "ix_candidates_status_position_id",
    "ix_candidates_position_status_score",
    "ix_candidates_skills_gin",
    "ux_messages_channel_external",
    "ix_messages_channel_direction_id",
    "ix_messages_retry",
    "ix_messages_external_id",
    "ix_events_object",
    "ix_email_threads_message_ids_gin",
    "ix_positions_code",
)


# --------------------------------------------------------------------- 输出小工具

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET} {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}FAIL{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * max(24, len(title) * 2))


# --------------------------------------------------------------------- 连接参数


@dataclass
class Target:
    """一套库的连接信息（从 URL 解析，避免手工拆串出错）。

    ``url`` 给 asyncpg 用（``postgresql://``），``sa_url`` 给 SQLAlchemy/Alembic 用
    （``postgresql+asyncpg://``）。两个都留着，是因为驱动名不一样：
    少了 ``+asyncpg`` 时 SQLAlchemy 会去找 psycopg2，报一个跟真实原因无关的
    ``No module named 'psycopg'``。
    """

    url: str
    label: str
    host: str = ""
    port: int = 5432
    user: str = ""
    password: str = ""
    database: str = ""

    @classmethod
    def parse(cls, url: str, label: str) -> Target:
        raw = url.strip()
        # SQLAlchemy 风格的 driver 前缀 asyncpg 不认，去掉
        for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql+psycopg2://"):
            if raw.startswith(prefix):
                raw = "postgresql://" + raw[len(prefix) :]
                break
        if not raw.startswith("postgresql://"):
            raise ValueError(f"只支持 postgresql:// 连接串，收到：{url[:20]}…")
        from urllib.parse import unquote, urlparse

        parts = urlparse(raw)
        if not parts.path or parts.path == "/":
            raise ValueError("连接串缺少库名（形如 postgresql://user:pass@host:5432/dbname）")
        return cls(
            url=raw,
            label=label,
            host=parts.hostname or "localhost",
            port=parts.port or 5432,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=parts.path.lstrip("/"),
        )

    @property
    def sa_url(self) -> str:
        return "postgresql+asyncpg://" + self.url[len("postgresql://") :]

    @property
    def describe(self) -> str:
        return f"{self.label} {self.user}@{self.host}:{self.port}/{self.database}"


# --------------------------------------------------------------------- 1. 备份


def pg_dump_available() -> list[str] | None:
    """找可用的 pg_dump：本机优先，其次项目 compose 的 postgres 容器，再退到 pgsql 容器。

    返回「执行前缀」列表（如 ``["docker", "exec", "pgsql", "pg_dump"]``）；
    找不到就返回 ``None``，由调用方决定是否放弃。
    """
    if shutil.which("pg_dump"):
        return ["pg_dump"]
    if not shutil.which("docker"):
        return None
    try:
        listed = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.split()
    except (subprocess.SubprocessError, OSError):
        return None
    for container in ("hr_ai_robot-postgres-1", "pgsql"):
        if container in listed:
            return ["docker", "exec", "-e", "PGPASSWORD", container, "pg_dump"]
    return None


def backup(target: Target, out_dir: Path) -> Path | None:
    """用 pg_dump 自定义格式备份；返回备份文件路径，失败返回 None。"""
    prefix = pg_dump_available()
    if prefix is None:
        warn("本机没有 pg_dump，也没有可用的 postgres 容器 —— 无法备份")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = out_dir / f"{target.database}_{target.host}_{stamp}.dump"

    host = target.host
    if prefix[0] == "docker" and host in {"localhost", "127.0.0.1", "::1"}:
        # 在容器里跑 pg_dump，容器自己的 localhost 是它自己，得换成宿主机别名
        host = "host.docker.internal"

    # 密码只走环境变量（PASSFILE 更安全，但 Windows 上路径转义坑更多，这里用 env）
    cmd = [*prefix, "-h", host, "-p", str(target.port), "-U", target.user, "-Fc", "-d", target.database]
    env = {**os.environ, "PGPASSWORD": target.password}
    try:
        with dest.open("wb") as fh:
            proc = subprocess.run(
                cmd, stdout=fh, stderr=subprocess.PIPE, env=env, timeout=600
            )
    except (subprocess.SubprocessError, OSError) as exc:
        warn(f"pg_dump 执行失败：{exc}")
        return None

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        warn(f"pg_dump 返回 {proc.returncode}：{detail[-1] if detail else '无输出'}")
        dest.unlink(missing_ok=True)
        return None
    if dest.stat().st_size == 0:
        warn("pg_dump 产出空文件，视为失败")
        dest.unlink(missing_ok=True)
        return None
    ok(f"备份 → {dest}（{dest.stat().st_size / 1024:.0f} KB）")
    info(f"恢复命令：pg_restore --clean --if-exists -d <库> {dest.name}")
    return dest


# --------------------------------------------------------------------- 2. 现状体检


async def table_counts(conn: asyncpg.Connection) -> dict[str, int]:
    existing = {
        row["tablename"]
        for row in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    counts: dict[str, int] = {}
    for table in BUSINESS_TABLES:
        if table in existing:
            counts[table] = await conn.fetchval(f'SELECT count(*) FROM "{table}"') or 0
    return counts


async def current_revision(conn: asyncpg.Connection) -> str:
    exists = await conn.fetchval(
        "SELECT to_regclass('public.alembic_version') IS NOT NULL"
    )
    if not exists:
        return ""
    return str(await conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1") or "")


async def schema_facts(conn: asyncpg.Connection) -> dict[str, object]:
    """查 JSON/时间列的方言，作为「优化是否真的生效」的硬证据。"""
    json_cols = await conn.fetch(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type IN ('json', 'jsonb')
        ORDER BY table_name, column_name
        """
    )
    time_cols = await conn.fetch(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type IN ('timestamp without time zone', 'timestamp with time zone')
        ORDER BY table_name, column_name
        """
    )
    indexes = {
        row["indexname"]
        for row in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )
    }
    return {
        "json": [r for r in json_cols if r["data_type"] == "json"],
        "jsonb": [r for r in json_cols if r["data_type"] == "jsonb"],
        "ts_naive": [r for r in time_cols if r["data_type"] == "timestamp without time zone"],
        "ts_aware": [r for r in time_cols if r["data_type"] == "timestamp with time zone"],
        "indexes": indexes,
    }


async def report(target: Target) -> tuple[bool, dict[str, object]]:
    """打印现状并给出结论；返回 (是否全部达标, 事实快照)。"""
    conn = await asyncpg.connect(target.url)
    try:
        version = await conn.fetchval("SHOW server_version")
        revision = await current_revision(conn)
        counts = await table_counts(conn)
        facts = await schema_facts(conn)
    finally:
        await conn.close()

    section(f"现状 · {target.describe}")
    info(f"PostgreSQL {version} · alembic revision = {revision or '（未迁移，空库）'}")
    if counts:
        info("数据量：" + "，".join(f"{k} {v}" for k, v in counts.items()))
    else:
        info("数据量：（没有任何业务表，空库）")

    healthy = True

    jsonb = facts["jsonb"]
    naive_json = facts["json"]
    if naive_json:
        bad(f"{len(naive_json)} 个列仍是 json（应为 jsonb）：{_names(naive_json)}")
        healthy = False
    elif jsonb:
        ok(f"{len(jsonb)} 个 JSON 列均为 jsonb")

    ts_naive = facts["ts_naive"]
    ts_aware = facts["ts_aware"]
    if ts_naive:
        bad(f"{len(ts_naive)} 个时间列仍是 timestamp without time zone：{_names(ts_naive)}")
        healthy = False
    elif ts_aware:
        ok(f"{len(ts_aware)} 个时间列均为 timestamptz")

    missing = [name for name in REQUIRED_INDEXES if name not in facts["indexes"]]
    if missing:
        bad(f"缺少 {len(missing)} 个索引：{', '.join(missing)}")
        healthy = False
    else:
        ok(f"{len(REQUIRED_INDEXES)} 个关键索引齐全")

    if not facts["indexes"]:
        healthy = False  # 空库：还没迁移，不能算达标

    # 唯一索引真的在挡重复吗——只看 DDL 不够，还要看有没有重复行残留
    if "messages" in counts:
        conn = await asyncpg.connect(target.url)
        try:
            dup = await conn.fetchval(
                """
                SELECT count(*) FROM (
                    SELECT channel, external_id FROM messages
                    WHERE external_id <> '' AND status IN ('received','sent','mocked')
                    GROUP BY channel, external_id HAVING count(*) > 1
                ) d
                """
            )
        finally:
            await conn.close()
        if dup:
            bad(f"messages 里仍有 {dup} 组重复 (channel, external_id)，唯一索引建不上")
            healthy = False
        else:
            ok("messages 无重复 (channel, external_id)")

    return healthy, {"revision": revision, "counts": counts, "facts": facts}


def _names(rows: object) -> str:
    rows = list(rows)  # type: ignore[arg-type]
    head = [f"{r['table_name']}.{r['column_name']}" for r in rows[:6]]
    return "、".join(head) + ("…" if len(rows) > 6 else "")


# --------------------------------------------------------------------- 3. 迁移


def alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    return str(ScriptDirectory.from_config(cfg).get_current_head())


def upgrade(target: Target) -> bool:
    """在目标库上执行 alembic upgrade head（URL 显式注入）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    # 用户可能只被允许 schema 权限，不需要 CREATEDB；这里只 ALTER TABLE
    cfg.set_main_option("sqlalchemy.url", target.sa_url.replace("%", "%%"))
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001 - 迁移失败要原样报出来
        bad(f"alembic upgrade 失败：{type(exc).__name__}: {exc}")
        return False
    ok(f"alembic upgrade head 完成（目标 {alembic_head()}）")
    return True


# --------------------------------------------------------------------- 4. 搬数据


async def copy_data(source_url: str, target: Target, assume_yes: bool) -> bool:
    """把源库 6 张业务表整表搬到目标库：先清空、再灌入、最后校准自增序列。

    语义是「让云端和本机一致」，不是「增量合并」——所以必须先清空。
    这一步会删目标库的数据，默认要求交互确认；CI/无人值守用 ``--yes``。
    """
    src = await asyncpg.connect(source_url)
    dst = await asyncpg.connect(target.url)
    try:
        src_counts = await table_counts(src)
        dst_counts = await table_counts(dst)
        total = sum(dst_counts.values())
        if total and not assume_yes:
            answer = input(
                f"\n目标库现有 {total} 行数据（{'，'.join(f'{k} {v}' for k, v in dst_counts.items())}）。\n"
                f"继续会**清空**它们，用源库数据覆盖。输入 yes 继续："
            ).strip()
            if answer.lower() != "yes":
                warn("已取消（未做任何改动）")
                return False

        for table in BUSINESS_TABLES:
            if table not in dst_counts and table not in src_counts:
                continue
            await dst.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')

        for table in BUSINESS_TABLES:
            if table not in src_counts:
                continue
            columns = [
                row["column_name"]
                for row in await src.fetch(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=$1
                    ORDER BY ordinal_position
                    """,
                    table,
                )
            ]
            rows = await src.fetch(f'SELECT * FROM "{table}"')
            if not rows:
                info(f"{table}：源库为空，跳过")
                continue
            await dst.copy_records_to_table(
                table, records=[tuple(r) for r in rows], columns=columns
            )
            await _reset_sequence(dst, table)
            ok(f"{table}：搬入 {len(rows)} 行")

        # 搬完对账：行数必须一一对上
        for table in BUSINESS_TABLES:
            if table not in src_counts:
                continue
            now = await dst.fetchval(f'SELECT count(*) FROM "{table}"')
            if now != src_counts[table]:
                bad(f"{table} 对账失败：源 {src_counts[table]} 行，目标 {now} 行")
                return False
        ok("六张表行数全部对账一致")
        return True
    finally:
        await src.close()
        await dst.close()


async def _reset_sequence(conn: asyncpg.Connection, table: str) -> None:
    """copy 进来的 id 不推进序列，下一次 INSERT 会撞主键——必须手工校准。"""
    seq = await conn.fetchval("SELECT pg_get_serial_sequence($1, 'id')", table)
    if not seq:
        return
    await conn.execute(
        f"SELECT setval('{seq}', COALESCE((SELECT max(id) FROM \"{table}\"), 1), "
        f"(SELECT max(id) FROM \"{table}\") IS NOT NULL)"
    )


# --------------------------------------------------------------------- 5. 结构快照（可选）

async def dump_ddl(target: Target, out_dir: Path) -> Path | None:
    """把目标库的表结构与索引写成一份可读 SQL，方便同事 review 或做 diff。"""
    conn = await asyncpg.connect(target.url)
    try:
        rows = await conn.fetch(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'public' ORDER BY indexname
            """
        )
    finally:
        await conn.close()
    if not rows:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{target.database}_indexes_{target.host}.sql"
    dest.write_text(
        "-- 目标库索引快照（同步脚本生成）\n"
        + "\n".join(f"{r['indexdef']};" for r in rows)
        + "\n",
        encoding="utf-8",
    )
    ok(f"索引快照 → {dest}（{len(rows)} 条）")
    return dest


# --------------------------------------------------------------------- 主流程


async def _amain(args: argparse.Namespace) -> int:
    try:
        target = Target.parse(args.target, "目标库")
    except ValueError as exc:
        bad(str(exc))
        return 2

    source_url = ""
    if args.copy_data:
        source_url = (args.source or "").strip()
        if not source_url:
            from config.settings import get_settings

            source_url = get_settings().database_url
            info(f"源库取 .env 的 DATABASE_URL（{_mask(source_url)}）")
        source_url = _to_asyncpg(source_url)

    # ① 现状
    healthy, snapshot = await report(target)
    if args.check:
        return 0 if healthy else 1

    revision = str(snapshot["revision"])
    counts = snapshot["counts"]  # type: ignore[assignment]
    has_data = bool(sum(counts.values()))  # type: ignore[arg-type]
    head = alembic_head()
    need_migrate = revision != head

    if not need_migrate and healthy and not args.copy_data:
        ok("结构与版本号都已是目标状态，无需改动")
        return 0

    # ② 备份（有数据就必须成功，否则拒绝继续 —— 这是硬闸门）
    out_dir = Path(args.backup_dir).expanduser()
    if has_data:
        section("备份")
        # pg_dump 是同步子进程，放线程里跑，别把事件循环堵住
        dumped = await asyncio.to_thread(backup, target, out_dir)
        if dumped is None and not args.skip_backup:
            bad("备份失败，拒绝继续改库（确认无需备份请加 --skip-backup）")
            return 2
    else:
        info("目标库为空，跳过备份")

    # ③ 迁移
    if need_migrate:
        section("迁移")
        info(f"{revision or '（空库）'} → {head}")
        # migrations/env.py 内部用 asyncio.run() 驱动异步引擎，
        # 在已有事件循环里会直接 RuntimeError —— 必须丢到线程里执行。
        if not await asyncio.to_thread(upgrade, target):
            # 迁移是原子的（每步一个事务），失败后状态仍可查
            print("\n迁移失败，请把上面的报错发出来；库已回滚到出错前的那一版。")
            return 1
    else:
        section("迁移")
        ok("已是最新版本，跳过 alembic")

    # ④ 搬数据
    if args.copy_data:
        section("搬数据（本机 → 目标库）")
        if not await copy_data(source_url, target, assume_yes=args.yes):
            return 1

    # ⑤ 复核
    healthy, _ = await report(target)

    section("留档")
    if await dump_ddl(target, out_dir / "ddl"):
        info("两端索引快照可 diff：同一份脚本对本机和云端各跑一次即可")

    print()
    if healthy:
        ok(f"同步完成：{target.describe}")
        return 0
    bad("同步后仍有检查项不通过，见上方 FAIL 行")
    return 1


def _to_asyncpg(url: str) -> str:
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def _mask(url: str) -> str:
    """日志里绝不出现明文口令。"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def main() -> int:
    # alembic 的 Config 会在导入阶段刷一屏 "setup plugin ..." 的 INFO 日志，
    # 把真正要紧的「PASS / FAIL」冲掉。升级过程本身的 INFO 保留（能看到跑了哪几版）。
    import logging

    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="把数据库结构（迁移 + 索引）同步到另一套库，并当场自证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--target",
        required=True,
        help="目标库连接串，如 postgresql://admin:pass@10.191.44.66:15432/hr_ai_robot",
    )
    parser.add_argument("--source", help="源库连接串（仅 --copy-data 用；默认取 .env）")
    parser.add_argument("--check", action="store_true", help="只体检，不做任何改动")
    parser.add_argument(
        "--copy-data",
        action="store_true",
        help="把源库 6 张业务表整表搬过去（会先清空目标表，语义是「让两边一致」）",
    )
    parser.add_argument("--yes", action="store_true", help="搬数据时跳过交互确认")
    parser.add_argument(
        "--backup-dir",
        default=str(BASE_DIR / "var" / "db_backup"),
        help="备份目录（默认 var/db_backup）",
    )
    parser.add_argument(
        "--skip-backup", action="store_true", help="有数据也跳过备份（仅在你另有备份时用）"
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
