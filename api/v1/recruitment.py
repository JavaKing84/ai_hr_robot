"""招聘自动化 HTTP 接口：手动触发、手动导入、查询留痕。

本层只做参数解析与依赖注入，业务全部在 ``service/``。
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from core.deps import Services, get_services
from core.paths import resolve_resume_path
from core.security import require_api_token
from core.exceptions import NotFoundError, ValidationFailed
from model.candidate import Candidate, CandidateStatus
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
from service.reporting import ReportingService

router = APIRouter(
    prefix="/recruitment",
    tags=["recruitment"],
    # 运维接口统一鉴权：配了 API_TOKEN 就必须带令牌（见 core/security.py）
    dependencies=[Depends(require_api_token)],
)


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


@router.get(
    "/candidates/{candidate_id}/resume",
    summary="简历预览（飞书里点开就能看）",
    response_class=HTMLResponse,
    response_model=None,
)
async def preview_resume(
    candidate_id: int,
    download: bool = Query(default=False, description="true = 下载原件；默认在线看"),
    services: Services = Depends(get_services),
) -> HTMLResponse | FileResponse:
    """HR 在飞书里点「查看简历」打开的页面。

    有原件（pdf/docx）就优先给原件：pdf 浏览器直接渲染，其余按附件下载；
    原件丢失（历史上项目目录搬过家）时，用库里的 ``resume_text`` 渲染成可读页面。
    """
    candidate = await services.session.get(Candidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"候选人 {candidate_id} 不存在")

    path = resolve_resume_path(candidate.resume_path)
    exists = bool(path and path.exists())
    if path and exists and (download or path.suffix.lower() == ".pdf"):
        return FileResponse(
            path,
            filename=candidate.resume_filename or path.name,
            media_type="application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream",
        )
    return HTMLResponse(_resume_html(candidate, exists))


def _resume_html(candidate: Candidate, has_file: bool) -> str:
    """把结构化抽取结果 + 简历正文渲染成一页（无外部静态资源，单文件即可打开）。"""
    def row(label: str, value: object) -> str:
        text = str(value or "").strip()
        return f"<tr><th>{escape(label)}</th><td>{escape(text) or '—'}</td></tr>" if text or label else ""

    skills = "、".join(candidate.skills or []) or "—"
    highlights = "；".join(candidate.project_highlights or []) or "—"
    body = (candidate.resume_text or "").strip() or "（这份简历没有正文：原件不在磁盘上，且入库时未保存文本）"
    note = "" if has_file else "<p class=warn>原件不在磁盘上，下面是入库时保存的纯文本。</p>"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(candidate.name or '候选人')} 的简历</title>
<style>
body{{margin:0;background:#f6f7f9;color:#1f2329;font:16px/1.7 -apple-system,"Microsoft YaHei",sans-serif}}
main{{max-width:820px;margin:0 auto;padding:24px 18px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#646a73;font-size:14px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;margin-bottom:18px}}
th,td{{text-align:left;padding:9px 14px;border-bottom:1px solid #eef0f3;font-size:14px;vertical-align:top}}
th{{width:96px;color:#646a73;font-weight:500;background:#fafbfc}}
pre{{white-space:pre-wrap;word-break:break-word;background:#fff;border-radius:10px;padding:16px 18px;font:14px/1.8 ui-monospace,Consolas,monospace;margin:0}}
.warn{{background:#fff7e6;border:1px solid #ffd8a8;color:#ad6800;padding:8px 12px;border-radius:8px;font-size:13px}}
</style></head><body><main>
<h1>{escape(candidate.name or '未命名候选人')}</h1>
<div class="sub">{escape(candidate.position_code or '未归位')}　综合分 {round(candidate.score or 0, 1)}　{escape(CandidateStatus.LABELS.get(candidate.status, candidate.status))}</div>
{note}
<table>
{row("邮箱", candidate.email)}{row("电话", candidate.phone)}{row("学历", candidate.education_level)}
{row("院校", candidate.school)}{row("专业", candidate.major)}{row("年限", candidate.years_experience)}
{row("技能", skills)}{row("项目", highlights)}{row("摘要", candidate.summary)}
{row("原件", candidate.resume_filename or "（无）")}
</table>
<pre>{escape(body)}</pre>
</main></body></html>"""


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


@router.get("/stats", summary="运营漏斗（只读，最近 N 天）")
async def funnel_stats(
    days: int = Query(7, ge=1, le=90, description="统计最近多少天"),
    daily: bool = Query(False, description="是否带上按天明细"),
    services: Services = Depends(get_services),
) -> dict:
    """给主管/HR 看的数字：现在每个人停在哪一步（stage_snapshot），以及每天的流水（daily）。

    之前想知道「这周过筛率」只能自己写 SQL——现在一个 GET 就有，且只读不越界。
    """
    reporting = ReportingService(
        services.session, timezone=services.container.settings.timezone
    )
    payload = (await reporting.funnel(days=days)).as_dict()
    if daily:
        payload["daily"] = await reporting.daily_counts(days=days)
    return payload


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
