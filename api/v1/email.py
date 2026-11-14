"""邮件相关内部接口：手动轮询、通道自检、投递一封“收到的邮件”（联调/演示用）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from client.mailbox import InboundEmail
from core.deps import Services, get_services
from core.locks import LOCK_INBOUND_POLL, advisory_lock
from core.logging import get_logger, log_extra
from core.security import require_api_token
from core.timeparse import now_utc_naive
from schema.candidate import (
    EmailPollResponse,
    EmailTestRequest,
    EmailTestResponse,
    SimulateInboundEmailRequest,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/email",
    tags=["email"],
    dependencies=[Depends(require_api_token)],
)


@router.post("/poll", response_model=EmailPollResponse, summary="手动触发一次收信")
async def poll_mailbox(services: Services = Depends(get_services)) -> EmailPollResponse:
    """手动收一次信。

    与定时任务共用同一把 PG 咨询锁：两边同时跑会把同一封邮件写两遍，
    撞上 ``messages`` 的幂等唯一索引（2026-11-14 实测：11 封邮件都入库了，
    接口却因为并发写冲突回了 500）。拿不到锁就返回空结果，这不是错误——
    说明「这一刻定时任务正在收」，等它收完即可。
    """
    async with advisory_lock(
        services.container.engine, LOCK_INBOUND_POLL, name="inbound-poll-http"
    ) as acquired:
        if not acquired:
            logger.info("email.poll_skipped", extra=log_extra(reason="定时轮询正在进行"))
            return EmailPollResponse()
        return await services.orchestrator.poll_inbound()


@router.post("/test", response_model=EmailTestResponse, summary="发送一封通道自检邮件")
async def send_test_email(
    payload: EmailTestRequest, services: Services = Depends(get_services)
) -> EmailTestResponse:
    mailbox = services.container.mailbox
    result = await mailbox.send(to=payload.to, subject=payload.subject, body_text=payload.body)
    logger.info(
        "mail.test_sent",
        extra=log_extra(to=payload.to, ok=result.ok, mode=result.mode, error=result.error),
    )
    return EmailTestResponse(
        ok=result.ok, mode=result.mode, message_id=result.message_id, error=result.error
    )


@router.post(
    "/simulate-inbound",
    response_model=EmailPollResponse,
    summary="模拟一封入站邮件（主管/候选人回复）",
)
async def simulate_inbound(
    payload: SimulateInboundEmailRequest, services: Services = Depends(get_services)
) -> EmailPollResponse:
    """不依赖真实 IMAP 即可跑通「回复 → 解析 → 发通知」链路（演示与自动化测试用）。"""
    email = InboundEmail(
        message_id=payload.message_id or f"<sim-{uuid.uuid4().hex[:12]}@local>",
        subject=payload.subject,
        from_address=payload.from_address.lower(),
        to_addresses=payload.to_addresses or [services.container.settings.hr_mailbox],
        in_reply_to=payload.in_reply_to,
        references=list(payload.references),
        body_text=payload.body,
        received_at=now_utc_naive(),
    )
    outcome = await services.inbound.handle_email(email)
    return EmailPollResponse(
        processed=1 if outcome.handled else 0,
        ignored=0 if outcome.handled else 1,
        details=[
            {
                "kind": outcome.kind,
                "handled": outcome.handled,
                "from": payload.from_address,
                **outcome.detail,
            }
        ],
    )
