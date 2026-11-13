"""运维可观测性：一个最小的指标出口。

没有引入 prometheus_client（多一个常驻依赖、多一套采集约定），只输出
Prometheus 文本格式的三类核心数字——**收信数、通过数、失败数**，够回答
「机器人今天干活了吗、有没有把邮件发丢」这两个问题。

指标口径全部来自数据库真实留痕，不额外维护内存计数器：
进程重启后数字仍然连续，也不会出现「计数器被重启清零」的假故障。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select

from core.deps import Services, get_services
from core.security import require_api_token
from model.candidate import Candidate, CandidateStatus
from model.message import Direction, Message
from service.reporting import ReportingService

router = APIRouter(
    prefix="/ops",
    tags=["ops"],
    dependencies=[Depends(require_api_token)],
)

_HELP = {
    "hr_candidates_total": ("gauge", "各状态候选人总数"),
    "hr_messages_total": ("counter", "各渠道/方向/状态的留痕条数"),
    "hr_pending_approval": ("gauge", "待 HR 确认的面试通知草稿数"),
    "hr_outbound_failed": ("gauge", "失败未补发的出站消息数"),
}


@router.get(
    "/metrics",
    summary="Prometheus 文本指标（收信数/通过数/失败数）",
    response_class=Response,
    response_model=None,
)
async def metrics(services: Services = Depends(get_services)) -> Response:
    session = services.session
    lines: list[str] = []

    rows = await session.execute(
        select(Candidate.status, func.count()).group_by(Candidate.status)
    )
    lines.append("# HELP hr_candidates_total 各状态候选人总数")
    lines.append("# TYPE hr_candidates_total gauge")
    for status, count in rows.all():
        lines.append(f'hr_candidates_total{{status="{status}"}} {int(count)}')

    rows = await session.execute(
        select(Message.channel, Message.direction, Message.status, func.count()).group_by(
            Message.channel, Message.direction, Message.status
        )
    )
    lines.append("# HELP hr_messages_total 各渠道/方向/状态的留痕条数")
    lines.append("# TYPE hr_messages_total counter")
    for channel, direction, status, count in rows.all():
        lines.append(
            f'hr_messages_total{{channel="{channel}",direction="{direction}",status="{status}"}} {int(count)}'
        )

    pending = await session.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.OUTBOUND,
            Message.status == "pending_approval",
        )
    )
    lines.append("# HELP hr_pending_approval 待 HR 确认的面试通知草稿数")
    lines.append("# TYPE hr_pending_approval gauge")
    lines.append(f"hr_pending_approval {int(pending.scalar_one())}")

    failed = await session.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.direction == Direction.OUTBOUND, Message.status == "failed")
    )
    lines.append("# HELP hr_outbound_failed 失败未补发的出站消息数")
    lines.append("# TYPE hr_outbound_failed gauge")
    lines.append(f"hr_outbound_failed {int(failed.scalar_one())}")

    rejected = await session.execute(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.status == CandidateStatus.SCREENED_REJECTED)
    )
    lines.append("# HELP hr_screened_rejected_total 累计淘汰数")
    lines.append("# TYPE hr_screened_rejected_total gauge")
    lines.append(f"hr_screened_rejected_total {int(rejected.scalar_one())}")

    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/funnel", summary="运营漏斗原始 JSON（与 /recruitment/stats 同源）")
async def funnel(
    days: int = 7, services: Services = Depends(get_services)
) -> dict:
    return (
        await ReportingService(
            services.session, timezone=services.container.settings.timezone
        ).funnel(days=days)
    ).as_dict()


__all__ = ["router"]
