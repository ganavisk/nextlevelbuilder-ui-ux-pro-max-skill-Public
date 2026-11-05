"""Test Lab API (admin only): simulated calls, a tool bench, the day board by
bay, and a live activity feed. See service.py for what a simulation does and
does not touch."""

from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import AuthContext, require_role
from app.core.config import Settings
from app.modules.conversation.providers.anthropic_llm import AnthropicLLMProvider
from app.modules.conversation.tools import booking_fields
from app.modules.scheduling.service import bay_count
from app.modules.tenants.repository import get_active_agent_config, get_tenant
from app.modules.tenants.schemas import MessageResponse
from app.modules.testlab import service
from app.modules.testlab.schemas import (
    ActivityOut,
    BoardOut,
    DiscardOut,
    LabStatus,
    MessageRequest,
    SessionOut,
    StartSessionRequest,
    ToolEventOut,
    ToolRunRequest,
    TurnOut,
)

router = APIRouter()
admin = require_role("admin")


def _deps(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "llm": request.app.state.llm_provider,
        "embeddings": request.app.state.embedding_provider,
        "calendar": request.app.state.calendar_provider,
        "redis": request.app.state.redis,
        "deadline_seconds": settings.tool_deadline_seconds,
    }


def _llm_available(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    provider = request.app.state.llm_provider
    return not isinstance(provider, AnthropicLLMProvider) or bool(settings.anthropic_api_key)


@router.get("/test-lab/status", response_model=LabStatus)
async def status(request: Request, auth: AuthContext = Depends(admin)) -> LabStatus:
    tenant = await get_tenant(auth.tenant_id)
    scheduling = (tenant.settings or {}).get("scheduling", {}) or {}
    return LabStatus(
        llm_available=_llm_available(request),
        tools=list(service.TOOL_NAMES),
        bays=bay_count(tenant),
        timezone=tenant.timezone,
        default_caller=service.DEFAULT_TEST_CALLER,
        services=[str(s.get("name")) for s in scheduling.get("services", []) if s.get("name")],
        booking_fields=booking_fields(tenant),
    )


@router.post("/test-lab/sessions", response_model=SessionOut)
async def start(
    body: StartSessionRequest, request: Request, auth: AuthContext = Depends(admin)
) -> SessionOut:
    tenant = await get_tenant(auth.tenant_id)
    config = await get_active_agent_config(auth.tenant_id)
    call = await service.start_session(
        tenant, config, caller_e164=body.caller_e164 or "", redis=request.app.state.redis
    )
    return SessionOut(
        session_id=call.id,
        caller_e164=call.caller_e164,
        first_message=config.first_message,
        started_at=call.started_at or datetime.now(UTC),
    )


@router.post("/test-lab/sessions/{session_id}/messages", response_model=TurnOut)
async def message(
    session_id: UUID, body: MessageRequest, request: Request, auth: AuthContext = Depends(admin)
) -> TurnOut:
    call = await service.get_session(auth.tenant_id, session_id)
    tenant = await get_tenant(auth.tenant_id)
    config = await get_active_agent_config(auth.tenant_id)
    turn = await service.send_message(
        tenant=tenant, config=config, call=call, text=body.text, **_deps(request)
    )
    return TurnOut(
        reply=turn.reply,
        tool_events=[ToolEventOut(**asdict(e)) for e in turn.tool_events],
        model=turn.model,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
    )


@router.post("/test-lab/sessions/{session_id}/tools/{tool}", response_model=ToolEventOut)
async def run_tool(
    session_id: UUID,
    tool: str,
    body: ToolRunRequest,
    request: Request,
    auth: AuthContext = Depends(admin),
) -> ToolEventOut:
    call = await service.get_session(auth.tenant_id, session_id)
    tenant = await get_tenant(auth.tenant_id)
    config = await get_active_agent_config(auth.tenant_id)
    event = await service.run_bench_tool(
        tenant=tenant,
        config=config,
        call=call,
        tool=tool,
        arguments=body.arguments,
        **_deps(request),
    )
    return ToolEventOut(**asdict(event))


@router.post("/test-lab/sessions/{session_id}/end", response_model=MessageResponse)
async def end(
    session_id: UUID, request: Request, auth: AuthContext = Depends(admin)
) -> MessageResponse:
    call = await service.get_session(auth.tenant_id, session_id)
    await service.end_session(auth.tenant_id, call, redis=request.app.state.redis)
    return MessageResponse(message="call ended; the summary appears once the worker runs")


@router.delete("/test-lab/sessions/{session_id}", response_model=DiscardOut)
async def discard(
    session_id: UUID, request: Request, auth: AuthContext = Depends(admin)
) -> DiscardOut:
    call = await service.get_session(auth.tenant_id, session_id)
    removed = await service.discard_session(
        auth.tenant_id,
        call,
        calendar=request.app.state.calendar_provider,
        redis=request.app.state.redis,
    )
    return DiscardOut(removed_appointments=removed)


@router.get("/test-lab/board", response_model=BoardOut)
async def board(
    auth: AuthContext = Depends(admin), day: date | None = Query(default=None)
) -> BoardOut:
    tenant = await get_tenant(auth.tenant_id)
    today = datetime.now(ZoneInfo(tenant.timezone)).date()
    result = await service.day_board(tenant, day or today)
    return BoardOut(
        day=result.day,
        timezone=result.timezone,
        bays=result.bays,
        opening_hours=result.opening_hours,
        appointments=[asdict(a) for a in result.appointments],  # type: ignore[misc]
    )


@router.get("/test-lab/activity", response_model=list[ActivityOut])
async def activity(
    auth: AuthContext = Depends(admin),
    after: datetime | None = None,
    limit: int = Query(default=40, ge=1, le=100),
) -> list[ActivityOut]:
    items = await service.recent_activity(auth.tenant_id, after=after, limit=limit)
    return [ActivityOut(**asdict(i)) for i in items]
