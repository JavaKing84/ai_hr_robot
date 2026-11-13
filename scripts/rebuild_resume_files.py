"""从数据库的 resume_text 重建简历文本副本（原件丢失时用）。

    python scripts/rebuild_resume_files.py --dry-run   # 只看会写哪些文件
    python scripts/rebuild_resume_files.py            # 真正写盘

写出的文件名为 ``<原文件名去扩展>.recovered.txt``，与原件区分开，不覆盖任何已有文件。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from config.settings import get_settings  # noqa: E402
from core.paths import resolve_resume_path  # noqa: E402


async def _run(dry_run: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    settings.resumes_dir.mkdir(parents=True, exist_ok=True)

    written = skipped = empty = 0
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "select id, name, resume_filename, resume_path, resume_text "
                    "from candidates order by id"
                )
            )
        ).all()

        existing = {p.name for p in settings.resumes_dir.glob("*")}
        for cid, name, filename, raw_path, body in rows:
            original = resolve_resume_path(raw_path)
            if original and original.exists():
                skipped += 1
                continue
            if not (body or "").strip():
                empty += 1
                continue
            stem = Path(filename or f"candidate-{cid}").stem
            target = settings.resumes_dir / f"{stem}.recovered.txt"
            if target.name in existing:
                skipped += 1
                continue
            print(f"[{cid:>3}] {name or '?':<10} -> {target.name} ({len(body)} 字符)")
            if not dry_run:
                target.write_text(f"# 候选人 {name or cid}｜原文件 {filename}\n\n{body}", encoding="utf-8")
            written += 1

    await engine.dispose()
    action = "将写入" if dry_run else "已写入"
    print(f"\n{action} {written} 个，跳过 {skipped} 个（原件已存在），无正文 {empty} 个")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="从 resume_text 重建简历文本副本")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = parser.parse_args()
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
