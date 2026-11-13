"""把库里已有的简历文件按指定岗位重新走一遍真实导入 + 筛选。

与 demo_ingest.py 的区别：这里不内置简历正文，全部从 ``var/resumes/`` 读真实文件，
符合「从库里导入」的做法。解析仍走真实管道（pypdf / text + 大模型抽取），
筛选走 ScreeningService，不做任何分数美化。

用法::

    python scripts/ingest_from_library.py                 # 用下面 PLAN 里的清单
    python scripts/ingest_from_library.py --list          # 只列出可导入文件
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deps import build_container, build_services, set_container  # noqa: E402

CYCLE_ID = 202611140520  # 本轮演练批次

RESUME_DIR = Path("var/resumes")

#: (岗位 code, 简历文件名) —— 文件都在 var/resumes 下
PLAN: list[tuple[str, str]] = [
    ("product_manager", "20261114051157-937ebe-赵敏-产品经理.txt"),
    ("product_manager", "20261114020604-ea05e9-陈静-产品经理.txt"),
    ("marketing_specialist", "20261114020606-0da263-周涛-市场运营专员.txt"),
]


async def main() -> int:
    parser = argparse.ArgumentParser("ingest_from_library")
    parser.add_argument("--list", action="store_true", help="只列出可导入的简历文件")
    args = parser.parse_args()

    if args.list:
        for p in sorted(RESUME_DIR.glob("*")):
            print(f"{p.stat().st_size:>8}  {p.name}")
        return 0

    container = set_container(build_container())
    print(f"mail_mode={container.settings.mail_mode}  feishu_mode={container.settings.feishu_mode}")
    print(f"cycle_id={CYCLE_ID}\n")

    rows: list[tuple[int, str, str, str, float]] = []
    async with container.session_factory() as session:
        services = build_services(session, container)

        for code, filename in PLAN:
            path = RESUME_DIR / filename
            if not path.exists():
                print(f"[skip] 文件不存在：{path}")
                continue
            outcome = await services.ingestion.ingest_resume_bytes(
                path.read_bytes(),
                filename,
                position_code=code,
                source="manual",
                cycle_id=CYCLE_ID,
            )
            cand = outcome.candidate
            scr = outcome.screening
            verdict = "—" if scr is None else ("通过" if scr.passed else "未通过")
            print(
                f"[ingest] {filename}\n"
                f"         id={getattr(cand, 'id', None)} created={outcome.created} "
                f"dup={outcome.is_duplicate} pos={outcome.position_code} "
                f"engine={getattr(cand, 'parse_engine', '')}\n"
                f"         筛选={verdict} score={getattr(scr, 'score', None)} "
                f"硬性未过={getattr(scr, 'hard_failed', None)}\n"
                f"         note={outcome.note}"
            )
        await session.commit()

        print("\n[本轮入库结果]")
        from sqlalchemy import select

        from model.candidate import Candidate

        stmt = select(Candidate).where(Candidate.cycle_id == CYCLE_ID).order_by(Candidate.score.desc())
        for c in (await session.execute(stmt)).scalars():
            rows.append((c.id, c.name, c.position_code, c.status, c.score))
            print(
                f"  {c.id}  {c.name:<6} {c.position_code:<22} {c.status:<16} "
                f"score={c.score:<6} edu={c.education_level or '空'} "
                f"yrs={c.years_experience}"
            )

    await container.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
