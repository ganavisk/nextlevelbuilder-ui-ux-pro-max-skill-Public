"""Dashboard-facing call history: list + detail with transcript. Separate
from `routes.py` (the Vapi webhook ingress) on purpose — different auth
model entirely (bearer JWT here, HMAC signature there) and different
callers (a human browsing calls vs. a voice vendor reporting them).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.auth import AuthContext, get_current_user
from app.core.db import tenant_session
from app.core.errors import NotFoundError
from app.core.util import Page
from app.modules.telephony.models import Call, Transcript
from app.modules.telephony.schemas import CallDetail, CallSummary, TranscriptTurn

router = APIRouter()


@router.get("/calls", response_model=Page[CallSummary])
async def list_calls(
    auth: AuthContext = Depends(get_current_user),
    outcome: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[CallSummary]:
    async with tenant_session(auth.tenant_id) as session:
        stmt = select(Call).where(Call.tenant_id == auth.tenant_id)
        count_stmt = select(func.count()).select_from(Call).where(Call.tenant_id == auth.tenant_id)
        if outcome:
            stmt = stmt.where(Call.outcome == outcome)
            count_stmt = count_stmt.where(Call.outcome == outcome)
        total = (await session.execute(count_stmt)).scalar_one()
        rows = (
            await session.execute(stmt.order_by(Call.started_at.desc()).limit(limit).offset(offset))
        ).scalars()
        calls = [CallSummary.model_validate(r) for r in rows]
    return Page(items=calls, total=total)


@router.get("/calls/{call_id}", response_model=CallDetail)
async def get_call(call_id: UUID, auth: AuthContext = Depends(get_current_user)) -> CallDetail:
    async with tenant_session(auth.tenant_id) as session:
        call = (
            await session.execute(
                select(Call).where(Call.id == call_id, Call.tenant_id == auth.tenant_id)
            )
        ).scalar_one_or_none()
        if call is None:
            raise NotFoundError(f"no call {call_id}")
        turns = (
            await session.execute(select(Transcript.turns).where(Transcript.call_id == call_id))
        ).scalar_one_or_none() or []
        return CallDetail(
            **CallSummary.model_validate(call).model_dump(),
            recording_url=call.recording_url,
            vendor_cost_cents=call.vendor_cost_cents,
            llm_cost_cents=call.llm_cost_cents,
            transcript=[TranscriptTurn(**turn) for turn in turns],
        )
