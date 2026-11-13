"""语义分 A/B 对照：同一批简历，分别按「纯规则」和「规则+语义」各算一遍。

用途：在把 ``SEMANTIC_SHADOW`` 改成 false 之前，先看清语义分会把谁救回来、
把谁推下去，再决定阈值要不要调。

特点：
  * **不碰数据库**：职位直接读 ``config/positions.yaml``，候选人只在内存里评估；
  * **不发信、不发飞书、不发面试通知**：只做解析 + 打分 + 打印 + 写 CSV；
  * 附件落盘到 ``var/ab_semantic/isolation/resumes``（隔离目录），不污染 ``var/resumes``；
  * 没有 key 时照常能跑，语义分一列显示 ``-``，相当于「纯规则基线」。

用法：
    .venv/Scripts/python.exe scripts/ab_semantic.py
    .venv/Scripts/python.exe scripts/ab_semantic.py --eml-dir var/boss_recheck/mock_inbox
    .venv/Scripts/python.exe scripts/ab_semantic.py --resume-dir 测试简历 --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在导入 Settings 之前改环境变量：把这批简历的临时文件全部关到隔离目录
DEFAULT_OUT = ROOT / "var" / "ab_semantic"
_early_out = DEFAULT_OUT
for _i, _arg in enumerate(sys.argv):
    if _arg == "--out" and _i + 1 < len(sys.argv):
        _early_out = Path(sys.argv[_i + 1])
os.environ["VAR_DIR"] = str(_early_out / "isolation")

from client.llm import LLMClient  # noqa: E402
from client.mailbox import parse_email_bytes  # noqa: E402
from client.resume_parser import ResumeParser  # noqa: E402
from config.settings import Settings  # noqa: E402
from model.candidate import Candidate  # noqa: E402
from service.email_threading import RESUME_ATTACHMENT_SUFFIXES  # noqa: E402
from service.extraction import ExtractionService  # noqa: E402
from service.ingestion import IngestionService, _fallback_name  # noqa: E402
from service.screening import ScreeningService, evaluate  # noqa: E402


def _candidate_from(ext, text: str) -> Candidate:
    """把抽好的字段塞进一个「不落库」的 Candidate，供语义分调用使用。"""
    return Candidate(
        name=ext.name or "（未识别）",
        email=ext.email or "",
        phone=ext.phone or "",
        education_level=ext.education_level or "",
        school=ext.school or "",
        major=ext.major or "",
        years_experience=float(ext.years_experience or 0),
        skills=list(ext.skills or []),
        project_highlights=list(ext.project_highlights or []),
        summary=ext.summary or "",
        resume_text=text,
    )


#: 这些不是简历（项目自带的说明文件），统一跳过
_SKIP_STEMS = ("使用说明", "readme", "说明", "索引")


def _is_resume_name(name: str) -> bool:
    stem = Path(name or "").stem.strip().lower()
    return bool(stem) and stem not in _SKIP_STEMS and not stem.startswith("使用说明")


async def _iter_resumes(args, settings: Settings, parser: ResumeParser):
    """产出 (来源文件名, 邮件主题, 附件名, 正文文本)。"""
    if args.eml_dir:
        for eml in sorted(Path(args.eml_dir).glob("*.eml"))[: args.limit or None]:
            email = parse_email_bytes(eml.read_bytes(), raw_path=str(eml))
            picked = [
                a
                for a in email.attachments
                if (a.filename or "").lower().endswith(RESUME_ATTACHMENT_SUFFIXES)
                and _is_resume_name(a.filename)
            ]
            for attachment in picked:
                try:
                    parsed = await parser.save_and_parse(attachment.data, attachment.filename)
                except Exception as exc:  # noqa: BLE001 - 单份失败不影响整批
                    print(f"  ! 解析失败 {attachment.filename}: {exc}")
                    continue
                yield eml.name, email.subject, attachment.filename, parsed.text
        return

    directory = Path(args.resume_dir or ROOT / "测试简历")
    for path in sorted(directory.iterdir())[: args.limit or None]:
        if not path.is_file() or path.suffix.lower() not in RESUME_ATTACHMENT_SUFFIXES:
            continue
        if not _is_resume_name(path.name):
            continue
        try:
            parsed = await parser.save_and_parse(path.read_bytes(), path.name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 解析失败 {path.name}: {exc}")
            continue
        yield path.name, path.name, path.name, parsed.text


async def main() -> int:
    ap = argparse.ArgumentParser(description="语义分 A/B 对照（只读，不发信）")
    ap.add_argument("--eml-dir", default="", help="从 .eml 邮件目录读取（默认 var/boss_recheck/mock_inbox）")
    ap.add_argument("--resume-dir", default="", help="改用普通简历目录（pdf/docx/txt）")
    ap.add_argument("--limit", type=int, default=0, help="最多看几份（0=全部）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    # 两个来源都没指定时，优先用 BOSS 那批 .eml，其次用项目自带的测试简历
    if not args.eml_dir and not args.resume_dir:
        default_eml = ROOT / "var" / "boss_recheck" / "mock_inbox"
        if default_eml.is_dir():
            args.eml_dir = str(default_eml)
        else:
            args.resume_dir = str(ROOT / "测试简历")

    settings = Settings()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = settings.load_company_config()
    positions = [p for d in config.departments for p in d.positions]
    parser = ResumeParser(settings)
    llm = LLMClient(settings)
    extraction = ExtractionService(settings, llm=llm, parser=parser)
    # 只借用 _resolve_position / _semantic_score 两个方法，不需要 session
    resolver = IngestionService.__new__(IngestionService)
    scorer = ScreeningService(None, llm=llm, company_name=config.company.name, settings=settings)

    print(f"邮件模式：{settings.mail_mode}　LLM：{'可用' if llm.enabled else '未启用（无 key 或 LLM_MODE=off）'}")
    print(f"影子模式：{settings.semantic_shadow}　语义权重：" + "、".join(
        f"{p.code}={p.screening.semantic_weight:g}" for p in positions
    ))
    print(f"输出目录：{out_dir}\n")

    rows: list[dict[str, object]] = []
    async for source, subject, filename, text in _iter_resumes(args, settings, parser):
        ext = await extraction.extract_candidate(
            text, filename=filename, positions=[(p.code, p.title) for p in positions]
        )
        position, match_note = resolver._resolve_position(
            text, ext, positions, subject=subject, filename=filename
        )
        row: dict[str, object] = {
            "来源": source,
            "附件": filename,
            # 姓名与入库逻辑保持一致：规则抽不到就用附件名兜底
            "姓名": ext.name or _fallback_name(filename, ext.email),
            "岗位": position.code if position else "（未归位）",
            "归位说明": match_note,
            "学历": ext.education_level or "-",
            "院校": ext.school or "-",
            "专业": ext.major or "-",
            "年限": round(float(ext.years_experience or 0), 1),
            "规则分": 0.0,
            "语义分": None,
            "融合分": 0.0,
            "规则判定": "未归位",
            "融合判定": "未归位",
            "语义分生效": "否",
            "说明": "",
        }

        if position is not None:
            rule_result = evaluate(ext.model_dump(), position.screening)
            semantic: float | None = None
            note = ""
            if rule_result.hard_failed:
                note = "硬条件未过，未调语义分"
            elif not llm.enabled:
                note = "无 key，仅规则分"
            else:
                semantic, note = await scorer._semantic_score(_candidate_from(ext, text), position)

            applied = not settings.semantic_shadow
            fused = evaluate(
                ext.model_dump(),
                position.screening,
                semantic_score=semantic,
                semantic_applied=applied,
                semantic_note=note,
            )
            row.update(
                {
                    "规则分": rule_result.score,
                    "语义分": "" if semantic is None else round(semantic, 1),
                    "融合分": fused.score,
                    "规则判定": "通过" if rule_result.passed else "淘汰",
                    "融合判定": "通过" if fused.passed else "淘汰",
                    "语义分生效": "是" if applied else "否（影子）",
                    "说明": fused.semantic_note or fused.reason,
                }
            )
        else:
            row["说明"] = match_note

        rows.append(row)
        print(
            f"{str(row['姓名'])[:8]:<10}|{str(row['岗位']):<18}|{str(row['学历']):<4}|"
            f"{str(row['专业'])[:12]:<14}|{str(row['年限']):>4}|"
            f"规则 {float(row['规则分']):>6.2f}|语义 {str(row['语义分']):>5}|"
            f"融合 {float(row['融合分']):>6.2f}|{row['规则判定']}"
        )

    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d%H%M")
    csv_path = out_dir / f"ab_{stamp}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["姓名"])
        writer.writeheader()
        writer.writerows(rows)

    passed_rule = sum(1 for r in rows if r["规则判定"] == "通过")
    passed_fused = sum(1 for r in rows if r["融合判定"] == "通过")
    print(f"\n合计 {len(rows)} 份：规则通过 {passed_rule} 份；融合通过 {passed_fused} 份")
    if settings.semantic_shadow:
        print("当前是影子模式：融合分只做参考，实际通过与否仍按规则判定。")
    if not settings.llm_enabled:
        print("当前没有可用 key（OPENAI_API_KEY 为空），语义分列为空，这一片就是「纯规则基线」。")
    print(f"明细已写入：{csv_path}")
    print("提示：本脚本只读简历、只打分，不会发邮件 / 飞书 / 面试通知。")
    await llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
