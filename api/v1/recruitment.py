"""招聘自动化 HTTP 接口：手动触发、手动导入、查询留痕。

本层只做参数解析与依赖注入，业务全部在 ``service/``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from core.deps import Services, get_services
from core.exceptions import NotFoundError, ValidationFailed
from model.candidate import Candidate
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from repository.message import MessageRepository
from repository.position import PositionRepository
from schema.candidate import (
    CandidateListResponse,
    CandidateRead,
    EventRead,
    IngestResponse,
    IngestTextRequest,
    MessageRead,
    PositionRead,
    RunCycleResponse,
)

router = APIRouter(prefix="/recruitment", tags=["recruitment"])


@router.post("/run", response_model=RunCycleResponse, summary="手动触发一轮完整闭环")
async def run_cycle(services: Services = Depends(get_services)) -> RunCycleResponse:
    return await services.orchestrator.run_daily_cycle()


@router.post("/ingest", response_model=IngestResponse, summary="手动导入一份简历文件")
async def ingest_file(
    file: UploadFile = File(..., description="简历文件（pdf/docx/txt）"),
    position_code: str | None = Form(None, description="指定职位代码；不填则自动归位"),
    services: Services = Depends(get_services),
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise ValidationFailed("上传文件为空")
    outcome = await services.ingestion.ingest_resume_bytes(
        data,
        file.filename or "resume.bin",
        position_code=position_code,
        source="manual",
    )
    return _ingest_response(outcome)


@router.post("/ingest/text", response_model=IngestResponse, summary="手动导入简历纯文本")
async def ingest_text(
    payload: IngestTextRequest, services: Services = Depends(get_services)
) -> IngestResponse:
    outcome = await services.ingestion.ingest_resume_bytes(
        payload.text.encode("utf-8"),
        payload.filename,
        position_code=payload.position_code,
        source="manual-text",
    )
    return _ingest_response(outcome)


@router.get("/candidates", response_model=CandidateListResponse, summary="查询候选人")
async def list_candidates(
    status: str | None = Query(None),
    position_code: str | None = Query(None),
    name: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    services: Services = Depends(get_services),
) -> CandidateListResponse:
    repo = CandidateRepository(services.session)
    items, total = await repo.search(
        status=status, position_code=position_code, name=name, limit=limit, offset=offset
    )
    tz = services.container.settings.timezone
    return CandidateListResponse(
        total=total, items=[CandidateRead.from_model(item, tz) for item in items]
    )


@router.get("/candidates/{candidate_id}", response_model=CandidateRead, summary="候选人详情")
async def get_candidate(candidate_id: int, services: Services = Depends(get_services)) -> CandidateRead:
    candidate = await services.session.get(Candidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"候选人 {candidate_id} 不存在")
    return CandidateRead.from_model(candidate, services.container.settings.timezone)


@router.get("/candidates/{candidate_id}/events", response_model=list[EventRead], summary="候选人事件流水")
async def candidate_events(
    candidate_id: int, services: Services = Depends(get_services)
) -> list[EventRead]:
    events = await EventRepository(services.session).list_for_object("candidate", candidate_id)
    return [
        EventRead(
            id=event.id,
            action=event.action,
            actor=event.actor,
            object_type=event.object_type,
            object_id=event.object_id,
            from_state=event.from_state,
            to_state=event.to_state,
            detail=event.detail or {},
            note=event.note,
            occurred_at=event.occurred_at,
        )
        for event in events
    ]


@router.get("/events", response_model=list[EventRead], summary="最近事件（全局留痕）")
async def recent_events(
    limit: int = Query(100, ge=1, le=500),
    action: str | None = Query(None),
    services: Services = Depends(get_services),
) -> list[EventRead]:
    events = await EventRepository(services.session).list_recent(limit=limit, action=action)
    return [
        EventRead(
            id=event.id,
            action=event.action,
            actor=event.actor,
            object_type=event.object_type,
            object_id=event.object_id,
            from_state=event.from_state,
            to_state=event.to_state,
            detail=event.detail or {},
            note=event.note,
            occurred_at=event.occurred_at,
        )
        for event in events
    ]


@router.get("/messages", response_model=list[MessageRead], summary="收发留痕（飞书/邮件）")
async def recent_messages(
    channel: str | None = Query(None),
    direction: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    services: Services = Depends(get_services),
) -> list[MessageRead]:
    messages = await MessageRepository(services.session).list_recent(
        channel=channel, direction=direction, limit=limit
    )
    return [
        MessageRead(
            id=message.id,
            channel=message.channel,
            direction=message.direction,
            sender=message.sender,
            recipient=message.recipient,
            subject=message.subject,
            body=message.body,
            external_id=message.external_id,
            status=message.status,
            candidate_id=message.candidate_id,
            position_id=message.position_id,
            parsed=message.parsed or {},
            error=message.error,
            created_at=message.created_at,
        )
        for message in messages
    ]


@router.get("/positions", response_model=list[PositionRead], summary="在招职位与主管映射")
async def list_positions(services: Services = Depends(get_services)) -> list[PositionRead]:
    positions = await PositionRepository(services.session).list_active()
    return [
        PositionRead(
            code=position.code,
            title=position.title,
            department=position.department,
            headcount=position.headcount,
            threshold=position.threshold,
            rule_version=position.rule_version,
            supervisor_name=position.supervisor_name,
            supervisor_title=position.supervisor_title,
            supervisor_email=position.supervisor_email,
            active=position.active,
        )
        for position in positions
    ]


def _ingest_response(outcome) -> IngestResponse:
    candidate = outcome.candidate
    if candidate is None:
        raise ValidationFailed(outcome.note or "简历未能入库")
    return IngestResponse(
        candidate_id=candidate.id,
        name=candidate.name,
        position_code=candidate.position_code,
        status=candidate.status,
        score=round(candidate.score or 0, 2),
        is_duplicate=outcome.is_duplicate,
        message=outcome.note,
    )


__all__ = ["router"]
