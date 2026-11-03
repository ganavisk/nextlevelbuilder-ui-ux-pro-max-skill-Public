"""Spoken-language handlers for the two scheduling tools.

These sit on the in-call hot path, so every failure mode degrades to
something a receptionist could plausibly say rather than an error: a calendar
outage becomes "let me take a message", a lost race becomes "that one just
went — here's what else is open" (Phase 1 Risk R9).

Returned strings are read aloud, so no ISO timestamps, no markdown.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.queue import CONFIRMATIONS_QUEUE, enqueue
from app.modules.scheduling.availability import describe_slots
from app.modules.scheduling.providers.base import CalendarProvider, TimeSlot
from app.modules.scheduling.service import (
    SlotTakenError,
    book_appointment,
    find_available_slots,
)
from app.modules.tenants.models import Tenant

logger = get_logger(__name__)

DEFAULT_DURATION_MINUTES = 30
SEARCH_HORIZON_DAYS = 14

COULD_NOT_CHECK = (
    "I'm having trouble reaching the calendar right now. Let me take your "
    "details and have someone call you back to get it booked."
)
NOTHING_AVAILABLE = (
    "I don't see anything open in the next couple of weeks. Let me take your "
    "details and have someone call you back with more options."
)


def _scheduling_config(tenant: Tenant) -> dict[str, Any]:
    return (tenant.settings or {}).get("scheduling", {}) or {}


def resolve_duration(tenant: Tenant, service: str) -> int:
    """Match the requested service to a configured duration, falling back to a
    sensible default rather than refusing — callers rarely say the service
    name exactly as configured."""
    services = _scheduling_config(tenant).get("services", []) or []
    wanted = (service or "").strip().lower()
    for entry in services:
        name = str(entry.get("name", "")).lower()
        if wanted and (wanted == name or wanted in name or name in wanted):
            return int(entry.get("duration_minutes", DEFAULT_DURATION_MINUTES))
    if services:
        return int(services[0].get("duration_minutes", DEFAULT_DURATION_MINUTES))
    return DEFAULT_DURATION_MINUTES


def parse_when(value: str | None, timezone: str) -> datetime | None:
    """Parse a model-supplied datetime. Naive values are interpreted in the
    tenant's timezone — the model is told the local time, so a bare
    '2026-09-15T09:00' means 9am for the business, not 9am UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(UTC)


async def handle_check_availability(
    *,
    tenant: Tenant,
    calendar: CalendarProvider,
    service: str,
    preferred_time: str | None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    duration = resolve_duration(tenant, service)
    min_notice = int(_scheduling_config(tenant).get("min_notice_hours", 0))

    preferred = parse_when(preferred_time, tenant.timezone)
    if preferred:
        # Search the preferred day, then widen — offering only the exact
        # minute the caller guessed would usually return nothing.
        local_day = preferred.astimezone(ZoneInfo(tenant.timezone)).date()
        window_start = datetime(
            local_day.year, local_day.month, local_day.day, tzinfo=ZoneInfo(tenant.timezone)
        ).astimezone(UTC)
        window_end = window_start + timedelta(days=1)
    else:
        window_start, window_end = now, now + timedelta(days=SEARCH_HORIZON_DAYS)

    try:
        slots = await find_available_slots(
            tenant=tenant,
            calendar=calendar,
            duration_minutes=duration,
            window_start=window_start,
            window_end=window_end,
            min_notice_hours=min_notice,
            now=now,
        )
        if not slots and preferred:
            slots = await find_available_slots(
                tenant=tenant,
                calendar=calendar,
                duration_minutes=duration,
                window_start=now,
                window_end=now + timedelta(days=SEARCH_HORIZON_DAYS),
                min_notice_hours=min_notice,
                now=now,
            )
    except Exception:
        logger.exception("availability_lookup_failed", tenant_id=str(tenant.id))
        return COULD_NOT_CHECK

    if not slots:
        return NOTHING_AVAILABLE

    spoken = describe_slots(slots, tenant.timezone, limit=3)
    return f"I have {spoken}. Which works best for you?"


def _idempotency_key(call_id: uuid.UUID | None, start: datetime, scope: str | None = None) -> str:
    """Scoped to the call and the slot: a retried webhook replays the same
    booking, while a genuine second booking on the same call gets its own key.

    Without a call id there is no retry identity to key on, so the key must be
    unique per attempt instead. Keying on the slot alone would make two
    unrelated callers requesting the same time look like one retry, and the
    second caller would be told they're booked into the first caller's
    appointment.
    """
    if scope is not None:
        return f"{scope}:{start.isoformat()}"
    if call_id is None:
        return f"anon:{uuid.uuid4()}:{start.isoformat()}"
    return f"call:{call_id}:{start.isoformat()}"


async def handle_book_appointment(
    *,
    tenant: Tenant,
    calendar: CalendarProvider,
    redis: Redis,
    call_id: uuid.UUID | None,
    service: str,
    starts_at: str,
    customer_name: str,
    customer_phone: str,
    customer_email: str | None,
    now: datetime | None = None,
    details: dict[str, Any] | None = None,
    send_confirmation: bool = True,
    idempotency_scope: str | None = None,
) -> str:
    now = now or datetime.now(UTC)
    start = parse_when(starts_at, tenant.timezone)
    if start is None:
        return "I didn't quite catch the time — could you say that again?"

    duration = resolve_duration(tenant, service)
    slot = TimeSlot(start=start, end=start + timedelta(minutes=duration))

    try:
        outcome = await book_appointment(
            tenant=tenant,
            calendar=calendar,
            call_id=call_id,
            slot=slot,
            service=service,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            idempotency_key=_idempotency_key(call_id, start, idempotency_scope),
            details=details,
        )
    except SlotTakenError:
        alternatives = await _safe_alternatives(tenant, calendar, duration, now)
        if alternatives:
            return (
                f"Ah — that one was just taken. I do have {alternatives}. Would any of those work?"
            )
        return "That time was just taken, and I don't see another opening. Let me take a message."
    except Exception:
        logger.exception("booking_failed", tenant_id=str(tenant.id))
        return COULD_NOT_CHECK

    when = describe_slots([slot], tenant.timezone, limit=1)
    if not send_confirmation:
        return f"You're all set for {when}."

    # Confirmations go out behind the call, never while the caller waits.
    await enqueue(
        redis,
        CONFIRMATIONS_QUEUE,
        {
            "type": "appointment_confirmation",
            "tenant_id": str(tenant.id),
            "appointment_id": str(outcome.appointment_id),
        },
    )
    channel = "an email and a text" if customer_email else "a text"
    return f"You're all set for {when}. I'll send you {channel} to confirm."


async def _safe_alternatives(
    tenant: Tenant, calendar: CalendarProvider, duration: int, now: datetime
) -> str:
    try:
        slots = await find_available_slots(
            tenant=tenant,
            calendar=calendar,
            duration_minutes=duration,
            window_start=now,
            window_end=now + timedelta(days=SEARCH_HORIZON_DAYS),
            min_notice_hours=int(_scheduling_config(tenant).get("min_notice_hours", 0)),
            now=now,
        )
    except Exception:
        logger.exception("alternatives_lookup_failed", tenant_id=str(tenant.id))
        return ""
    return describe_slots(slots, tenant.timezone, limit=2)
