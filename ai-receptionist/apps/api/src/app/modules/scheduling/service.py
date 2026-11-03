"""Booking: availability lookup and the write path that cannot double-book.

## Why the database is the mutex

No calendar API offers an atomic "create this event only if the slot is
free". Two callers can both pass an availability check milliseconds apart and
both attempt to book. So the serialization point is our own exclusion
constraint: no two confirmed appointments for one tenant may overlap in the
same bay (migration 0008; it replaced 0005's same-start-time index).

    1. Insert the appointment row first  ← the lock, claiming the lowest
       free bay. A conflict moves on to the next bay; when every bay
       conflicts the caller lost the race and is offered alternatives.
    2. Only the winner writes to the calendar.
    3. If the calendar write then fails, the reservation is released so the
       slot does not stay phantom-booked.

Doing it the other way round (calendar first) cannot be serialized, and
failing to release on error is how "ghost" appointments appear.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.db import table_of, tenant_session
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.modules.scheduling.availability import available_slots
from app.modules.scheduling.models import Appointment
from app.modules.scheduling.providers.base import (
    BookingRequest,
    CalendarProvider,
    TimeSlot,
)
from app.modules.tenants.models import Tenant

logger = get_logger(__name__)


class SlotTakenError(Exception):
    """The slot was booked by someone else between offer and confirmation."""


def bay_count(tenant: Tenant) -> int:
    """How many appointments the business can run at once
    (`settings.scheduling.bays`; 1 when unset, e.g. a single-chair practice)."""
    scheduling = (tenant.settings or {}).get("scheduling", {}) or {}
    try:
        return max(1, int(scheduling.get("bays", 1)))
    except (TypeError, ValueError):
        return 1


@dataclass(frozen=True)
class BookingOutcome:
    appointment_id: uuid.UUID
    slot: TimeSlot
    already_existed: bool


async def find_available_slots(
    *,
    tenant: Tenant,
    calendar: CalendarProvider,
    duration_minutes: int,
    window_start: datetime,
    window_end: datetime,
    min_notice_hours: int = 0,
    now: datetime | None = None,
) -> list[TimeSlot]:
    """The tenant's business hours, minus calendar busy periods (which block
    every bay), keeping slots where at least one bay is free of our own
    confirmed appointments.

    Both sources matter: the calendar may hold events booked elsewhere, and
    our own table may hold a booking whose calendar write is still in flight.
    """
    now = now or datetime.now(UTC)
    busy = list(await calendar.list_busy_periods(tenant.id, window_start, window_end))

    async with tenant_session(tenant.id) as session:
        rows = (
            await session.execute(
                select(Appointment.starts_at, Appointment.ends_at, Appointment.bay).where(
                    Appointment.tenant_id == tenant.id,
                    Appointment.status == "confirmed",
                    Appointment.ends_at > window_start,
                    Appointment.starts_at < window_end,
                )
            )
        ).all()
    bay_bookings: dict[int, list[TimeSlot]] = {}
    for r in rows:
        bay_bookings.setdefault(r.bay, []).append(TimeSlot(start=r.starts_at, end=r.ends_at))

    return available_slots(
        business_hours=tenant.business_hours,
        timezone=tenant.timezone,
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
        busy_periods=busy,
        now=now,
        min_notice_hours=min_notice_hours,
        bay_bookings=bay_bookings,
        bays=bay_count(tenant),
    )


async def book_appointment(
    *,
    tenant: Tenant,
    calendar: CalendarProvider,
    call_id: uuid.UUID | None,
    slot: TimeSlot,
    service: str,
    customer_name: str,
    customer_phone: str,
    customer_email: str | None,
    idempotency_key: str,
    details: dict[str, Any] | None = None,
) -> BookingOutcome:
    """Reserve a bay, then write to the calendar. Raises SlotTakenError if
    every bay was taken between the offer and this booking."""
    existing = await _find_by_idempotency_key(tenant.id, idempotency_key)
    if existing is not None:
        logger.info("booking_replayed", appointment_id=str(existing))
        return BookingOutcome(appointment_id=existing, slot=slot, already_existed=True)

    bays = bay_count(tenant)
    # ── 1. Reserve. The exclusion constraint decides each bay's winner. ──
    reserved = await _reserve_first_free_bay(
        tenant=tenant,
        bays=bays,
        call_id=call_id,
        slot=slot,
        service=service,
        customer_name=customer_name,
        customer_phone=customer_phone,
        customer_email=customer_email,
        idempotency_key=idempotency_key,
        details=details or {},
    )
    if isinstance(reserved, BookingOutcome):
        return reserved  # this exact booking was inserted concurrently: a replay
    appointment_id, bay = reserved

    # ── 2. Calendar write, now that we hold the slot. ──
    try:
        result = await calendar.book(
            BookingRequest(
                tenant_id=tenant.id,
                slot=slot,
                summary=_event_summary(service, customer_name, bay if bays > 1 else None),
                attendee_name=customer_name,
                attendee_phone=customer_phone,
                attendee_email=customer_email,
                idempotency_key=idempotency_key,
                blocks_time=bays == 1,
            )
        )
    except Exception:
        # ── 3. Release, so a failed calendar write doesn't hold the slot. ──
        await _release(tenant.id, appointment_id)
        logger.exception("booking_calendar_write_failed", appointment_id=str(appointment_id))
        raise

    async with tenant_session(tenant.id) as session:
        await session.execute(
            table_of(Appointment)
            .update()
            .where(Appointment.id == appointment_id)
            .values(external_event_id=result.external_event_id)
        )

    logger.info(
        "appointment_booked",
        appointment_id=str(appointment_id),
        bay=bay,
        starts_at=slot.start.isoformat(),
        external_event_id=result.external_event_id,
    )
    return BookingOutcome(appointment_id=appointment_id, slot=slot, already_existed=False)


def _event_summary(service: str, customer_name: str, bay: int | None) -> str:
    title = f"{service} — {customer_name}".strip(" —")
    return f"Bay {bay} · {title}" if bay is not None else title


async def _reserve_first_free_bay(
    *,
    tenant: Tenant,
    bays: int,
    call_id: uuid.UUID | None,
    slot: TimeSlot,
    service: str,
    customer_name: str,
    customer_phone: str,
    customer_email: str | None,
    idempotency_key: str,
    details: dict[str, Any],
) -> tuple[uuid.UUID, int] | BookingOutcome:
    """Insert the appointment into bay 1, 2, ... until one is free.

    Each attempt is its own transaction, so a conflict in bay 1 does not abort
    the attempt on bay 2, and it holds a per-bay advisory lock (see below). A
    conflict can also be this very booking, inserted concurrently by a retried
    webhook; the idempotency lookup tells the two apart and turns that case
    into a replay rather than a false "taken".
    """
    for bay in range(1, bays + 1):
        try:
            async with tenant_session(tenant.id) as session:
                # One inserter per (tenant, bay) at a time. An exclusion
                # constraint inserts first and checks second, so two
                # simultaneous conflicting inserts can each wait on the other
                # and Postgres aborts one as a deadlock, which would reach the
                # caller as "trouble with the calendar" instead of "taken".
                # (The old unique index had btree's own handling for this.)
                # The lock is released at commit; the constraint stays the
                # guarantee.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"booking:{tenant.id}:{bay}"},
                )
                appointment = Appointment(
                    tenant_id=tenant.id,
                    call_id=call_id,
                    service=service,
                    starts_at=slot.start,
                    ends_at=slot.end,
                    status="confirmed",
                    idempotency_key=idempotency_key,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    customer_email=customer_email,
                    timezone=tenant.timezone,
                    bay=bay,
                    details=details,
                )
                session.add(appointment)
                await session.flush()
                return appointment.id, bay
        except IntegrityError:
            replay = await _find_by_idempotency_key(tenant.id, idempotency_key)
            if replay is not None:
                return BookingOutcome(appointment_id=replay, slot=slot, already_existed=True)
            continue  # this bay is taken for (part of) the slot; try the next
    logger.info("slot_taken", tenant_id=str(tenant.id), starts_at=slot.start.isoformat())
    raise SlotTakenError(str(slot.start))


async def _find_by_idempotency_key(tenant_id: uuid.UUID, key: str) -> uuid.UUID | None:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                select(Appointment.id).where(
                    Appointment.tenant_id == tenant_id, Appointment.idempotency_key == key
                )
            )
        ).scalar_one_or_none()


async def _release(tenant_id: uuid.UUID, appointment_id: uuid.UUID) -> None:
    """Free the reservation. Status moves out of 'confirmed', which drops it
    from the partial unique index so the slot is immediately rebookable."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(Appointment)
            .update()
            .where(Appointment.id == appointment_id)
            .values(status="cancelled", idempotency_key=None)
        )


async def cancel_appointment(
    *, tenant_id: uuid.UUID, appointment_id: uuid.UUID, calendar: CalendarProvider
) -> Appointment:
    """Dashboard-initiated cancellation — distinct from `_release` in intent
    (undoing a real, successful booking, not rolling back a failed one) even
    though the DB write is the same. Calendar-first here, unlike booking:
    there is no concurrent "other cancellation" to race, so there's no
    reservation to protect by writing our own row first.
    """
    async with tenant_session(tenant_id) as session:
        appointment = (
            await session.execute(
                select(Appointment).where(
                    Appointment.id == appointment_id, Appointment.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if appointment is not None:
            session.expunge(appointment)
    if appointment is None:
        raise NotFoundError(f"no appointment {appointment_id}")
    if appointment.status != "confirmed":
        # Already cancelled/completed/no-show: cancelling again is a no-op,
        # not an error — the dashboard button being clicked twice (a slow
        # network, an impatient double-click) must not surface a failure.
        return appointment

    if appointment.external_event_id:
        await calendar.cancel(tenant_id, appointment.external_event_id)
    await _release(tenant_id, appointment_id)

    async with tenant_session(tenant_id) as session:
        updated = (
            await session.execute(select(Appointment).where(Appointment.id == appointment_id))
        ).scalar_one()
        session.expunge(updated)
        return updated


def default_window(now: datetime, days: int = 14) -> tuple[datetime, datetime]:
    return now, now + timedelta(days=days)
