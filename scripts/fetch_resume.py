"""按候选人查简历原件并还原到本地目录（离线取件工具）。

背景：``candidates`` 表里**不存文件二进制**，只存三个指针——
``resume_path``（落盘绝对路径）、``resume_filename``（原始文件名）、
``resume_text``（提取出的全文，最多 5 万字符）。所以「拿某个人的简历」=
先查行拿指针，再把指针指向的文件复制出来。

用法：
    # 用姓名模糊查（会列出所有命中，让你挑）
    python scripts/fetch_resume.py --name 谷金玉

    # 精确按邮箱 / 手机号 / 主键查
    python scripts/fetch_resume.py --email gujinyu@example.com
    python scripts/fetch_resume.py --id 128 --out D:/resumes

    # 一次导出某个职位的全部候选人简历
    python scripts/fetch_resume.py --position-code backend_senior --all

    # 文件已经丢了：用库里的 resume_text 重建一份 .txt（有损，仅应急）
    python scripts/fetch_resume.py --id 128 --rebuild-text

设计要点：
1. 只读，不写数据库、不改候选人状态。
2. 三条兜底：``resume_path`` 为空（解析子进程失败）→ 用 ``resume_text`` 重建；
   路径是容器内绝对路径（``/app/var/resumes/...``）→ 退化成「按 basename
   在当前 ``var/resumes`` 里找」；文件被删 → 报出来，不静默跳过。
3. 同名多份时按 ``id`` 前缀命名，避免互相覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROUTE = Path(__file__).resolve().parents[1]
if str(_ROUTE) not in sys.path:  # 允许 `python scripts/xxx.py` 直接运行
    sys.path.insert(0, str(_ROUTE))

from scripts._bootstrap import bootstrap  # noqa: E402

from sqlalchemy import or_, select  # noqa: E402

from model.candidate import Candidate  # noqa: E402


def _describe(candidate: Candidate) -> str:
    """一行摘要，用来在多条命中时让人挑。"""
    return (
        f"id={candidate.id}｜{candidate.name or '未知'}｜"
        f"{candidate.email or '-'}｜{candidate.phone or '-'}｜"
        f"{candidate.position_code or '未归位'}｜{candidate.status}"
    )


def _resolve_file(candidate: Candidate, resumes_dir: Path) -> tuple[Path | None, str]:
    """把 ``resume_path`` 解析成本机真实存在的文件。

    返回 ``(路径, 说明)``；找不到时路径为 ``None``，说明里写清原因。
    ``resumes_dir`` 只在「绝对路径失效」时用于按文件名兜底查找。
    """
    raw = (candidate.resume_path or "").strip()
    if not raw:
        return None, "resume_path 为空（多半是解析子进程失败，只剩 resume_text）"

    path = Path(raw)
    if path.is_file():
        return path, "直接命中 resume_path"

    # 容器里写入的是 /app/var/resumes/xxx，宿主机上这个绝对路径不存在；
    # 退化成「同名文件在当前 resumes_dir 下找」——Docker 场景通常要把卷挂出来。
    local = resumes_dir / path.name
    if local.is_file():
        return local, f"绝对路径失效，按文件名在 {resumes_dir} 找到"

    return None, f"文件不存在：{raw}"


def _write_rebuilt_text(candidate: Candidate, out_dir: Path) -> Path | None:
    """用 ``resume_text`` 重建一份 .txt（有损兜底：拿不回排版与原件）。"""
    text = (candidate.resume_text or "").strip()
    if not text:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{candidate.id}-{candidate.name or '未命名'}-重建.txt"
    header = (
        f"# 由数据库 candidates.resume_text 重建（有损，非原件）\n"
        f"# id={candidate.id} 姓名={candidate.name} 原文件={candidate.resume_filename}\n"
        f"# 原 resume_path={candidate.resume_path or '(空)'}\n"
        f"# 重建时间={datetime.now().isoformat(timespec='seconds')}\n\n"
    )
    target.write_text(header + text, encoding="utf-8")
    return target


async def main(args: argparse.Namespace) -> int:
    if not any([args.id, args.name, args.email, args.phone, args.position_code]):
        print("至少要给一个条件：--id / --name / --email / --phone / --position-code")
        return 2

    container = bootstrap()
    resumes_dir = container.settings.resumes_dir
    out_dir = Path(args.out).expanduser().resolve() if args.out else (Path.cwd() / "var" / "export")
    out_dir.mkdir(parents=True, exist_ok=True)

    conditions = []
    if args.id:
        conditions.append(Candidate.id == int(args.id))
    if args.name:
        conditions.append(Candidate.name.ilike(f"%{args.name}%"))
    if args.email:
        conditions.append(Candidate.email == args.email.strip().lower())
    if args.phone:
        conditions.append(Candidate.phone == args.phone.strip())
    if args.position_code:
        conditions.append(Candidate.position_code == args.position_code)

    async with container.session_factory() as session:
        stmt = select(Candidate).where(or_(*conditions)).order_by(Candidate.id)
        rows = list((await session.execute(stmt)).scalars().all())

    if not rows:
        print("没有匹配的候选人。换个条件，或用 --name 的关键字模糊查。")
        await container.aclose()
        return 1

    if len(rows) > 1 and not args.all:
        print(f"命中 {len(rows)} 条，未指定 --all，先列出来（加 --id 或 --all 再取）：")
        for row in rows:
            print("  " + _describe(row))
        await container.aclose()
        return 0

    copied = rebuilt = missing = 0
    seen: set[str] = set()
    for candidate in rows:
        source, note = _resolve_file(candidate, resumes_dir)
        if source is not None:
            # 同一份文件被多条记录引用时只复制一次（重复投递会把路径写在首条上）
            key = str(source.resolve())
            if key in seen and not args.all:
                continue
            seen.add(key)
            suffix = source.suffix or ".bin"
            target = out_dir / f"{candidate.id}-{candidate.name or '未命名'}{suffix}"
            shutil.copy2(source, target)
            copied += 1
            print(f"[原件] {_describe(candidate)}\n        {source} → {target}\n        {note}")
            continue

        if args.rebuild_text:
            rebuilt_path = _write_rebuilt_text(candidate, out_dir)
            if rebuilt_path is not None:
                rebuilt += 1
                print(f"[重建] {_describe(candidate)}\n        {note}\n        → {rebuilt_path}")
                continue
        missing += 1
        print(f"[缺失] {_describe(candidate)}\n        {note}")

    print(
        f"\n完成：原件 {copied} 份，重建 {rebuilt} 份，缺失 {missing} 份；输出目录 {out_dir}"
    )
    if missing and not args.rebuild_text:
        print("提示：缺失的可加 --rebuild-text 用 resume_text 重建（有损，仅应急）。")
    await container.aclose()
    return 0 if missing == 0 else 3


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="按候选人查简历原件（只读）")
    parser.add_argument("--id", type=int, help="候选人主键")
    parser.add_argument("--name", help="姓名关键字（模糊匹配）")
    parser.add_argument("--email", help="邮箱（精确匹配，自动小写）")
    parser.add_argument("--phone", help="手机号（精确匹配）")
    parser.add_argument("--position-code", help="职位代码，配合 --all 批量导出")
    parser.add_argument("--all", action="store_true", help="命中多条时全部导出")
    parser.add_argument("--out", help="输出目录，默认 ./var/export")
    parser.add_argument(
        "--rebuild-text",
        action="store_true",
        help="原件缺失时用 candidates.resume_text 重建 .txt（有损）",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
