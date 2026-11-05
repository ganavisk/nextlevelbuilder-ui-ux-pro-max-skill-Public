"""Dashboard-facing callback queue: the human side of the v1 voicemail-plus-
callback escalation policy — list requests, mark them worked."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, literal_column, select

from app.core.auth import AuthContext, get_current_user, require_role
from app.core.db import tenant_session
from app.core.util import Page
from app.modules.escalation.models import CallbackRequest
from app.modules.escalation.schemas import CallbackRequestSummary, CallbackStatusUpdate
from app.modules.escalation.service import update_callback_status

router = APIRouter()


@router.get("/callback-requests", response_model=Page[CallbackRequestSummary])
async def list_callback_requests(
    auth: AuthContext = Depends(get_current_user),
    status: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[CallbackRequestSummary]:
    async with tenant_session(auth.tenant_id) as session:
        stmt = select(CallbackRequest).where(CallbackRequest.tenant_id == auth.tenant_id)
        count_stmt = (
            select(func.count())
            .select_from(CallbackRequest)
            .where(CallbackRequest.tenant_id == auth.tenant_id)
        )
        if status:
            stmt = stmt.where(CallbackRequest.status == status)
            count_stmt = count_stmt.where(CallbackRequest.status == status)
        total = (await session.execute(count_stmt)).scalar_one()
        # Open requests first regardless of age — that's the actual triage
        # order a receptionist works the queue in, not creation time.
        rows = (
            await session.execute(
                # Within each group, newest first. (This used to order by
                # id, a random UUID, so the queue's order was arbitrary.)
                stmt.order_by(
                    # A literal, not a bound parameter: Postgres only uses the
                    # index on (status = 'resolved') when the query's
                    # expression is identical (migration 0012).
                    (CallbackRequest.status == literal_column("'resolved'")).asc(),
                    CallbackRequest.created_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        items = [CallbackRequestSummary.model_validate(r) for r in rows]
    return Page(items=items, total=total)


@router.patch("/callback-requests/{request_id}", response_model=CallbackRequestSummary)
async def update_callback(
    request_id: UUID,
    body: CallbackStatusUpdate,
    auth: AuthContext = Depends(require_role("admin")),
) -> CallbackRequestSummary:
    updated = await update_callback_status(auth.tenant_id, request_id, body.status)
    return CallbackRequestSummary.model_validate(updated)
