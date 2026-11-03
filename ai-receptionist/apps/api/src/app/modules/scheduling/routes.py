"""Dashboard-facing appointment list + cancellation. Nothing here runs on
the in-call hot path — that's `scheduling/tools.py`, a different module
boundary entirely (voice agent vs. human staff)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.core.auth import AuthContext, get_current_user, require_role
from app.core.db import tenant_session
from app.core.util import Page
from app.modules.scheduling.models import Appointment
from app.modules.scheduling.schemas import AppointmentSummary
from app.modules.scheduling.service import cancel_appointment
from app.modules.telephony.models import Call

router = APIRouter()


@router.get("/appointments", response_model=Page[AppointmentSummary])
async def list_appointments(
    auth: AuthContext = Depends(get_current_user),
    status: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[AppointmentSummary]:
    async with tenant_session(auth.tenant_id) as session:
        stmt = (
            select(Appointment, Call.direction)
            .outerjoin(Call, Call.id == Appointment.call_id)
            .where(Appointment.tenant_id == auth.tenant_id)
        )
        count_stmt = (
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.tenant_id == auth.tenant_id)
        )
        if status:
            stmt = stmt.where(Appointment.status == status)
            count_stmt = count_stmt.where(Appointment.status == status)
        total = (await session.execute(count_stmt)).scalar_one()
        rows = (
            await session.execute(
                stmt.order_by(Appointment.starts_at.desc()).limit(limit).offset(offset)
            )
        ).all()
        items = [_summary(a, direction) for a, direction in rows]
    return Page(items=items, total=total)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentSummary)
async def cancel(
    appointment_id: UUID, request: Request, auth: AuthContext = Depends(require_role("admin"))
) -> AppointmentSummary:
    calendar = request.app.state.calendar_provider
    appointment = await cancel_appointment(
        tenant_id=auth.tenant_id, appointment_id=appointment_id, calendar=calendar
    )
    async with tenant_session(auth.tenant_id) as session:
        direction = (
            await session.execute(select(Call.direction).where(Call.id == appointment.call_id))
        ).scalar_one_or_none()
    return _summary(appointment, direction)


def _summary(appointment: Appointment, direction: str | None) -> AppointmentSummary:
    summary = AppointmentSummary.model_validate(appointment)
    summary.simulated = direction == "simulated"
    return summary
