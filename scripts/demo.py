"""一条命令跑完整个招聘闭环（全 mock，不依赖飞书/邮箱/LLM 任何凭证）。

    python scripts/demo.py            # 从干净数据库开始，逐步骤打印发生了什么
    python scripts/demo.py --keep     # 不动已有数据，直接在当前库上再跑一轮

覆盖的链路：
    简历投递 → 解析 → 筛选 → 按部门打包（飞书 + 邮件副本）
      → 主管在飞书回复「名单 + 时间」→ 给候选人发面试通知邮件
      → 候选人邮件改期 → 请示主管 → 主管同意 → 补发调整确认
每一步都打印机器人真正发出去的东西（飞书文本 / 邮件正文），便于人工验收。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._bootstrap import bootstrap  # noqa: E402
from scripts.ingest_sample import SAMPLES, _email_bytes  # noqa: E402

from client.mailbox import InboundEmail  # noqa: E402
from core.db import init_models  # noqa: E402
from core.deps import build_services  # noqa: E402
from core.logging import configure_logging  # noqa: E402
from core.timeparse import format_interview_time  # noqa: E402
from model.base import utcnow_naive  # noqa: E402
from model.candidate import Candidate, CandidateStatus  # noqa: E402
from model.message import Channel, Direction, Message  # noqa: E402
from repository.position import PositionRepository  # noqa: E402
from schema.message import InboundMessage  # noqa: E402
from sqlalchemy import select  # noqa: E402

LINE = "─" * 78


def title(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


def bullet(text: str) -> None:
    print(f"  · {text}")


def quote(text: str, limit: int = 900) -> None:
    body = (text or "").strip()
    if len(body) > limit:
        body = body[:limit] + "\n  …（已截断，完整内容见 var/outbox/mail/*.eml 或 messages 表）"
    for raw in body.splitlines():
        print(f"    │ {raw}")


def feishu_event(*, open_id: str, text: str, chat_id: str = "oc_demo") -> dict:
    """构造一条与飞书 im.message.receive_v1 完全同构的事件报文。"""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    return {
        "schema": "2.0",
        "header": {"event_id": uuid.uuid4().hex, "event_type": "im.message.receive_v1", "create_time": now_ms, "token": ""},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {
                "message_id": f"om_demo_{uuid.uuid4().hex[:8]}",
                "chat_id": chat_id,
                "message_type": "text",
                "content": __import__("json").dumps({"text": text}, ensure_ascii=False),
                "create_time": now_ms,
            },
        },
    }


def show_detail(detail: dict, tz: str) -> None:
    """打印服务返回的 detail；时间字段转成北京时间，避免看到裸 UTC 误判。"""
    for key, value in detail.items():
        moment: datetime | None = None
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, str) and len(value) >= 19 and value[10] in "T ":
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                moment = None
        if moment is not None:
            bullet(f"{key} = {format_interview_time(moment, tz)}")
        else:
            bullet(f"{key} = {value}")


async def _outbound_emails(session, *, to_suffix: str | None = None) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.channel == Channel.EMAIL, Message.direction == Direction.OUTBOUND)
        .order_by(Message.id.desc())
        .limit(20)
    )
    rows = list((await session.execute(stmt)).scalars())
    if to_suffix:
        rows = [r for r in rows if to_suffix in (r.recipient or "")]
    return rows


async def main(keep: bool) -> None:
    container = bootstrap()
    settings = container.settings
    await init_models(container.engine)
    print(f"公司：{settings.load_company_config().company.name}")
    print(f"模式：飞书={settings.feishu_mode}｜邮件={settings.mail_mode}｜LLM={'on' if settings.llm_enabled else 'off'}")

    # ---------------------------------------------------------------- 步骤 0
    title("步骤 0／准备：同步职位与主管映射")
    if not keep:
        from scripts.seed import reset_tables  # 复用同一个建库逻辑

        await reset_tables(container)
        # 清掉上一轮遗留的模拟投递邮件与水位线，避免重复计入
        for stale in settings.mock_inbox_dir.glob("*.eml"):
            stale.unlink()
        (settings.mock_inbox_dir / ".state.json").unlink(missing_ok=True)
        bullet("已重建数据表并清空模拟收件箱")
    async with container.session_factory() as session:
        await PositionRepository(session).sync_from_config(settings.load_company_config())
        await session.commit()

    # ---------------------------------------------------------------- 步骤 1
    title("步骤 1／模拟候选人投递简历（写入模拟收件箱）")
    if keep:
        bullet("--keep：不写入新投递，直接在当前数据上再跑一轮（用于验证重跑/幂等）")
    else:
        for sample in SAMPLES:
            (settings.mock_inbox_dir / f"sample-{sample['name']}.eml").write_bytes(_email_bytes(sample))
        bullet(f"{len(SAMPLES)} 封投递邮件已写入 {settings.mock_inbox_dir}")

    # ---------------------------------------------------------------- 步骤 2
    title("步骤 2／跑一轮完整闭环：收简历 → 筛选 → 按部门打包发给主管")
    async with container.session_factory() as session:
        services = build_services(session, container)
        cycle = await services.orchestrator.run_daily_cycle()
    bullet(f"批次 cycle_id={cycle.cycle_id}｜收到 {cycle.ingested} 份｜通过 {cycle.screened_passed}｜淘汰 {cycle.screened_rejected}｜重复 {cycle.duplicates}")
    for pkg in cycle.packages:
        mark = "✓" if pkg.ok else "✗"
        feishu = "已发" if pkg.feishu_message_id else "未发"
        mail = "已发" if pkg.email_message_id else "未发"
        bullet(f"{mark} {pkg.supervisor} / {pkg.position_title}：收到 {pkg.received_count} 份、通过 {pkg.passed_count} 份（{('、'.join(pkg.candidate_names)) or '—'}）→ 飞书{feishu}｜主管邮件{mail}")
        if pkg.error:
            bullet(f"    ! {pkg.error}")
    if cycle.errors:
        for err in cycle.errors:
            bullet(f"! {err}")

    # 打印技术部主管收到的飞书消息 + 本轮生成的压缩包
    async with container.session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.channel == Channel.FEISHU, Message.direction == Direction.OUTBOUND)
                    .order_by(Message.id)
                )
            ).scalars()
        )
    target = next((r for r in rows if r.recipient == "ou_mock_zhangwei"), rows[0] if rows else None)
    title("步骤 2 产物 A／主管在飞书上收到的消息（技术部为例）")
    if target is not None:
        bullet(f"发给 {target.recipient}｜类型 文本 + 简历压缩包文件")
        quote(target.body)
    title("步骤 2 产物 B／本轮发给主管的简历压缩包")
    for pkg in cycle.packages:
        path = Path(pkg.package_path) if pkg.package_path else None
        if path and path.exists():
            bullet(f"{path.relative_to(_ROOT)}（{path.stat().st_size // 1024} KB，{pkg.passed_count} 份简历）")

    # ---------------------------------------------------------------- 步骤 3
    if keep:
        title("提示／--keep 模式到此为止")
        bullet("保留数据重跑时，上一轮已进入「已通知面试」的候选人不会再被安排一次，")
        bullet("因此步骤 3～5（主管给名单 → 发通知 → 改期）跳过；想完整走一遍请直接运行：")
        bullet("  .venv\\Scripts\\python scripts\\demo.py")
        await show_summary(container, settings)
        await container.aclose()
        return

    title("步骤 3／技术部主管直接在飞书上回复名单 + 时间")
    text = "我已经筛选审核完毕，请通知张明、李四这些人，于下周三下午 14:00 点来公司面试"
    bullet(f"主管原话：{text}")
    async with container.session_factory() as session:
        services = build_services(session, container)
        inbound = container.feishu.normalize_event(feishu_event(open_id="ou_mock_zhangwei", text=text))
        outcome = await services.feedback.handle(inbound)
    bullet(f"机器人判定动作：{outcome.action}｜已处理={outcome.handled}")
    if outcome.reply_text:
        bullet(f"机器人回给主管的话：{outcome.reply_text}")
    show_detail(outcome.detail, settings.timezone)

    async with container.session_factory() as session:
        rows = await _outbound_emails(session)
        sent = [r for r in rows if "面试" in (r.subject or "")]
    title("步骤 3 产物／发给候选人的面试通知邮件")
    for row in sent[:3]:
        bullet(f"To {row.recipient}｜{row.subject}")
        quote(row.body, 700)

    # ---------------------------------------------------------------- 步骤 4
    title("步骤 4／候选人回邮件要求改期")
    body = "您好，我下周二临时有会，能否改到周四下午 3 点？抱歉给您添麻烦。"
    bullet(f"候选人原话：{body}")
    async with container.session_factory() as session:
        # 找到发给张明的面试通知，取它的 Message-ID 作为 In-Reply-To（模拟真实「回复原邮件」）
        notice = next((r for r in await _outbound_emails(session) if "zhangming@" in (r.recipient or "") and "面试" in (r.subject or "")), None)
        email = InboundEmail(
            message_id=f"<reply-{uuid.uuid4().hex[:10]}@example.com>",
            subject=f"Re: {notice.subject}" if notice else "Re: 面试通知",
            from_address="zhangming@example.com",
            to_addresses=[settings.hr_mailbox],
            in_reply_to=notice.external_id if notice else "",
            references=[notice.external_id] if notice else [],
            body_text=body,
            received_at=utcnow_naive(),
        )
        services = build_services(session, container)
        handled = await services.inbound.handle_email(email)
    bullet(f"分流结果：{handled.kind}｜已处理={handled.handled}")
    show_detail(handled.detail, settings.timezone)

    async with container.session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.channel == Channel.FEISHU, Message.direction == Direction.OUTBOUND)
                    .order_by(Message.id.desc())
                    .limit(1)
                )
            ).scalars()
        )
    title("步骤 4 产物／机器人向主管请示（飞书）")
    for row in rows:
        quote(row.body)

    # ---------------------------------------------------------------- 步骤 5
    title("步骤 5／主管同意改期")
    async with container.session_factory() as session:
        services = build_services(session, container)
        inbound = container.feishu.normalize_event(feishu_event(open_id="ou_mock_zhangwei", text="同意，按他说的新时间安排"))
        outcome = await services.feedback.handle(inbound)
    bullet(f"机器人判定动作：{outcome.action}｜已处理={outcome.handled}")
    show_detail(outcome.detail, settings.timezone)
    async with container.session_factory() as session:
        rows = [r for r in await _outbound_emails(session) if "调整" in (r.subject or "")]
    title("步骤 5 产物／发给候选人的「面试时间调整确认」邮件")
    for row in rows[:2]:
        bullet(f"To {row.recipient}｜{row.subject}")
        quote(row.body, 600)

    # ---------------------------------------------------------------- 汇总
    await show_summary(container, settings)
    await container.aclose()


async def show_summary(container, settings) -> None:
    title("最终状态／候选人台账")
    async with container.session_factory() as session:
        candidates = list((await session.execute(select(Candidate).order_by(Candidate.id))).scalars())
    for c in candidates:
        label = CandidateStatus.LABELS.get(c.status, c.status)
        when = format_interview_time(c.interview_time, settings.timezone) if c.interview_time else "—"
        bullet(f"{c.name or '（未识别）'}｜{c.position_code or '—'}｜{label}｜面试时间 {when}｜分数 {c.score if c.score is not None else '—'}")
    print("\n落盘位置：邮件副本 var/outbox/mail/｜简历包 var/packages/｜数据库 var/hr_robot.db")


if __name__ == "__main__":
    import logging

    # 演示面向人工阅读：默认压到 WARNING，把版面留给「机器人到底发了什么」
    configure_logging()
    parser = argparse.ArgumentParser(description="招聘闭环一键演示（全 mock）")
    parser.add_argument("--keep", action="store_true", help="保留现有数据，不重建库")
    parser.add_argument("--verbose", action="store_true", help="同时打印结构化运行日志")
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.INFO if args.verbose else logging.WARNING)
    asyncio.run(main(args.keep))
