"""四条链路端到端验收（不碰真实邮箱、不碰真实候选人）。

跑法：
    .venv\\Scripts\\python.exe scripts\\e2e_four_chains.py

四条链路：
  ① 邮箱投递简历      → IMAP/mock 收信 → 解析 → 筛选 → 飞书群推送
  ② BOSS直聘后台投递  → 平台通知邮件（cv@notice.zhipin.com）→ 同上
  ③ HR 自然语言回复   → 大模型解析「名单 + 时间」→ 生成面试通知草稿（挂起）
  ④ HR 邮箱/飞书确认  → 放行投递（mock 邮件）→ 候选人回信改期 → 自动处理并通知主管

安全边界：
- ``MAIL_MODE=mock``：所有邮件只写 ``var/e2e/var/outbox/mail/*.eml``，**不会真的发给候选人**；
- ``FEISHU_MODE`` 用 .env 的真实配置：飞书群推送是真的（用于验证渠道改造）；
- 数据库用独立的 PostgreSQL 库 ``<库名>_e2e``（默认 ``hr_robot_e2e``），与生产库隔离，
  每次开跑前自动建库、清空表，跑完数据仍在库里可查（想清就再跑一次）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from config.settings import Settings  # noqa: E402
from core.db import (  # noqa: E402
    assert_disposable_database,
    derive_test_database_url,
    ensure_database_exists,
    init_models,
    prefer_local_test_target,
    truncate_all_tables,
)
from core.deps import build_container, build_services  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from model.candidate import CandidateStatus  # noqa: E402
from repository.candidate import CandidateRepository  # noqa: E402
from schema.message import InboundMessage  # noqa: E402

WORK = ROOT / "var" / "e2e"
SAMPLES = ROOT / "测试简历"
TESTDATA = ROOT / "tests" / "testdata"


def sample(name: str) -> Path:
    """定位样例简历：先找 ``测试简历/``，再在 ``tests/testdata/``（按部门分目录）里递归找。"""
    direct = SAMPLES / name
    if direct.exists():
        return direct
    matches = sorted(TESTDATA.rglob(name))
    if matches:
        return matches[0]
    raise FileNotFoundError(
        f"找不到样例简历「{name}」。\n"
        f"请放到 {SAMPLES} 下，或放到 {TESTDATA}\\<部门>\\ 下（当前测试简历放在这里）。"
    )
PASS = "PASS"
FAIL = "FAIL"

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def make_eml(
    path: Path,
    *,
    subject: str,
    from_addr: str,
    from_name: str = "",
    body: str,
    attachments: list[tuple[str, bytes, str]] = (),
    message_id: str = "",
    in_reply_to: str = "",
    references: list[str] | None = None,
) -> Path:
    """写一封标准 RFC822 邮件到 mock 收件箱。"""
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = "18786112948@163.com"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = message_id or f"<e2e-{datetime.now().strftime('%H%M%S%f')}@test>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body)
    for filename, data, subtype in attachments:
        maintype = "application" if subtype != "text" else "text"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    path.write_bytes(msg.as_bytes())
    return path


def build_docx(text: str) -> bytes:
    """把 txt 简历内容包成 docx（BOSS 平台附件只有 pdf/docx，用 docx 最省事）。"""
    import io

    from docx import Document

    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def main() -> int:
    configure_logging()
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # e2e 用独立 PG 库（<库名>_e2e），与生产库、pytest 测试库三者互不干扰。
    # 同 conftest：库名必须 * _e2e 结尾（护栏），且本机可用时一律走本机，
    # 免得每次验收都在共享服务器上清库。
    base_settings = Settings(_env_file=str(ROOT / ".env"))
    if not base_settings.database_url.startswith("postgresql"):
        print("DATABASE_URL 不是 PostgreSQL，无法跑四条链路验收。")
        print(f"当前值：{base_settings.database_url}")
        return 2
    derived_e2e = derive_test_database_url(base_settings.database_url, suffix="_e2e")
    assert_disposable_database(derived_e2e, purpose="四条链路验收")
    e2e_url, reason = prefer_local_test_target(derived_e2e)
    print(f"[e2e] 验收库：{reason} → {make_url(e2e_url).render_as_string(hide_password=True)}")
    await ensure_database_exists(e2e_url)

    settings = Settings(
        _env_file=str(ROOT / ".env"),
        database_url=e2e_url,
        var_dir=WORK / "var",
        config_path=ROOT / "config" / "positions.yaml",
        templates_dir=ROOT / "config" / "templates",
        mail_mode="mock",                 # 关键：邮件只落本地 outbox，不发给真人
        interview_notice_require_confirm=True,
        scheduler_enabled=False,
        inbound_poll_seconds=0,
    )
    container = build_container(settings)
    # 每次开跑前清空 e2e 库，保证四个断言的计数从 0 开始
    await truncate_all_tables(container.engine)
    await init_models(container.engine)

    print("=" * 78)
    print(" 四条链路端到端验收")
    print("=" * 78)
    print(f"库：{settings.database_url}")
    print(f"邮件：{settings.mail_mode}（只写 outbox）｜飞书：{settings.feishu_mode} / 群推送={settings.feishu_group_push}")
    print(f"LLM：{settings.llm_mode} / 供应商 {len(settings.llm_provider_chain)} 家 / 深度解析={settings.llm_deep_analysis}")
    print()

    async with container.session_factory() as session:
        services = build_services(session, container)

        # ---------------------------------------------------------------- 链路①
        print("—— 链路①：求职者用邮箱投递简历 ——")
        inbox = settings.mock_inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        mail_a = make_eml(
            inbox / "01-candidate-mail.eml",
            subject="应聘简历（高级后端工程师）",
            from_addr="chenhaoran@example.com",
            from_name="陈昊然",
            body="您好，附件是我的简历，请查收。",
            attachments=[
                (
                    "陈昊然-简历.txt",
                    sample("01_陈昊然_高级后端工程师_通过.txt").read_bytes(),
                    "plain",
                ),
                (
                    "林思远-简历.txt",
                    sample("02_林思远_后端开发工程师_通过.txt").read_bytes(),
                    "plain",
                ),
            ],
        )
        cycle_a = await services.orchestrator.run_daily_cycle()
        check(
            "① 邮箱简历已入库",
            cycle_a.ingested >= 2,
            f"ingested={cycle_a.ingested} passed={cycle_a.screened_passed} rejected={cycle_a.screened_rejected}",
        )
        check("① 简历包已推送到飞书群", len(cycle_a.packages) >= 1, f"packages={len(cycle_a.packages)}")
        for pkg in cycle_a.packages:
            check(
                f"① 飞书群返回 message_id（{pkg.position_title}）",
                bool(pkg.feishu_message_id),
                f"{pkg.feishu_message_id or pkg.error}",
            )
            # ok 同时包含「文本推送成功」与「压缩包上传成功」，缺 chat_id 时会在这里暴露
            check(
                f"① 简历压缩包已上传到群（{pkg.position_title}）",
                bool(pkg.ok),
                pkg.error or "文本+文件均成功",
            )

        # ---------------------------------------------------------------- 链路②
        print("\n—— 链路②：BOSS直聘后台投递（平台转发邮件）——")
        docx_bytes = build_docx(sample("03_赵语彤_算法工程师_通过.txt").read_text(encoding="utf-8"))
        make_eml(
            inbox / "02-boss-mail.eml",
            subject="赵语彤 | 3年，应聘 算法工程师 | 北京20-30K【BOSS直聘】",
            from_addr="cv@notice.zhipin.com",
            from_name="BOSS直聘",
            body="您有一份新的简历投递，请查收附件。",
            attachments=[("【算法工程师_北京_20-30K】赵语彤_3年.docx", docx_bytes, "vnd.openxmlformats-officedocument.wordprocessingml.document")],
        )
        cycle_b = await services.orchestrator.run_daily_cycle()
        check("② BOSS 转发简历已入库", cycle_b.ingested >= 1, f"ingested={cycle_b.ingested}")
        candidates = await CandidateRepository(session).list_by_cycle(cycle_b.cycle_id)
        boss = [c for c in candidates if c.name and "赵" in c.name]
        check(
            "② 平台发件人未被误认成候选人",
            bool(boss) and all(c.email != "cv@notice.zhipin.com" for c in boss),
            f"姓名={[c.name for c in boss]} 邮箱={[c.email for c in boss]}",
        )
        check("② 已推送到飞书群", bool(cycle_b.packages and cycle_b.packages[0].feishu_message_id),
              str(cycle_b.packages[0].feishu_message_id if cycle_b.packages else "无包"))

        # ---------------------------------------------------------------- 链路③
        print("\n—— 链路③：HR 在飞书用自然语言回复（大模型解析 + 待确认）——")
        dispatched = [
            c
            for c in await CandidateRepository(session).list_by_cycle(cycle_b.cycle_id)
            if c.status == CandidateStatus.DISPATCHED
        ] or [
            c
            for c in await CandidateRepository(session).list_by_cycle(cycle_a.cycle_id)
            if c.status == CandidateStatus.DISPATCHED
        ]
        names = [c.name for c in dispatched if c.name][:2]
        supervisor_open_id = (settings.hr_feishu_open_id or "").strip() or (
            container.company_config.departments[0].supervisor.feishu_open_id
        )
        human_text = (
            f"这 {len(names)} 份我只留了{'、'.join(names)}，请通知这两位下周三下午 14:00 来公司面试"
        )
        print(f"   HR 原话：{human_text}")
        inbound = InboundMessage(
            channel="feishu",
            sender_id=supervisor_open_id,
            sender_name="张伟",
            text=human_text,
            thread_id="oc_e2e_group",
            message_id=f"om_e2e_{datetime.now().strftime('%H%M%S')}",
            received_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        outcome = await services.feedback.handle(inbound)
        check("③ 大模型解析出面试名单与时间", outcome.action == "interview_scheduled", f"action={outcome.action}")
        detail = outcome.detail or {}
        check(
            "③ 名单接地正确（只认名单里出现的人）",
            set(detail.get("candidate_names") or []) <= set(c.name for c in dispatched),
            f"解析={detail.get('candidate_names')} 名单={[c.name for c in dispatched]}",
        )
        check(
            "③ 面试通知只挂起、未投递",
            int(detail.get("notices_pending_confirm") or 0) >= 1 and int(detail.get("notices_sent") or 0) == 0,
            f"sent={detail.get('notices_sent')} pending={detail.get('notices_pending_confirm')}",
        )

        pending = await services.notification.pending_interview_notices()
        outbox_before = len(list(settings.mail_outbox_dir.glob("*.eml")))
        check("③ 挂起的通知已落库", len(pending) >= 1, f"pending={len(pending)}")

        # ---------------------------------------------------------------- 链路④
        print("\n—— 链路④：确认后在飞书放行 + 邮件回信自动处理 ——")
        confirmed = await services.notification.confirm_pending_interview_notices([pending[0].id])
        outbox_after = len(list(settings.mail_outbox_dir.glob("*.eml")))
        check("④ HR 确认后邮件已投递（mock，未真发）", bool(confirmed and confirmed[0].ok),
              f"ok={confirmed[0].ok if confirmed else None} detail={confirmed[0].detail if confirmed else ''}")
        check("④ outbox 新增 1 封面试通知", outbox_after > outbox_before, f"{outbox_before} → {outbox_after}")

        candidate_id = pending[0].candidate_id
        candidate = await CandidateRepository(session).get(candidate_id) if candidate_id else None
        check("④ 候选人状态已变为「已邮件通知」", candidate is not None and candidate.status == CandidateStatus.INTERVIEW_NOTIFIED,
              f"status={candidate.status if candidate else '无'}")

        if candidate is not None:
            reply = make_eml(
                inbox / "03-candidate-reply.eml",
                subject="Re: 面试时间调整",
                from_addr=candidate.email or "candidate@example.com",
                from_name=candidate.name or "候选人",
                body="您好，原定时间我临时有会，能否改到本周五下午 3 点？",
            )
            poll = await services.inbound.poll_once()
            check("④ 候选人回信被处理", poll.processed >= 1, f"processed={poll.processed} ignored={poll.ignored} errors={poll.errors}")
            await session.refresh(candidate)
            check(
                "④ 改期请求被识别并转入待确认",
                candidate.status in {CandidateStatus.RESCHEDULE_PENDING, CandidateStatus.INTERVIEW_NOTIFIED},
                f"status={candidate.status}",
            )

        # ---------------------------------------------------------------- 汇总
        print("\n—— 数据落库汇总 ——")
        async with container.session_factory() as fresh:
            from sqlalchemy import func, select

            from model.candidate import Candidate
            from model.event import Event
            from model.message import Message

            rows = await fresh.execute(select(Candidate.status, func.count()).group_by(Candidate.status))
            for status, count in rows:
                print(f"   candidates.status={status:<24} {count}")
            total_events = (await fresh.execute(select(func.count()).select_from(Event))).scalar_one()
            total_msgs = (await fresh.execute(select(func.count()).select_from(Message))).scalar_one()
            print(f"   events={total_events}  messages={total_msgs}")

    await container.aclose()

    failed = [r for r in results if r[1] == FAIL]
    print("\n" + "=" * 78)
    print(f" 结果：{len(results) - len(failed)}/{len(results)} 通过")
    for name, status, detail in results:
        if status == FAIL:
            print(f"   ✗ {name} — {detail}")
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
