"""邮件相关内部接口：手动轮询、通道自检、投递一封“收到的邮件”（联调/演示用）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from client.mailbox import InboundEmail
from core.deps import Services, get_services
from core.logging import get_logger, log_extra
from core.timeparse import now_utc_naive
from schema.candidate import (
    EmailPollResponse,
    EmailTestRequest,
    EmailTestResponse,
    SimulateInboundEmailRequest,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/email", tags=["email"])


@router.post("/poll", response_model=EmailPollResponse, summary="手动触发一次收信")
async def poll_mailbox(services: Services = Depends(get_services)) -> EmailPollResponse:
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
