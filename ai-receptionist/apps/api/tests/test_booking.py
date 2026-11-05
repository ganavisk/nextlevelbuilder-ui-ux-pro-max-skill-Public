"""Booking write path, against real Postgres.

The headline guarantee of this phase is that two callers cannot be sold the
same slot. That cannot be tested with mocks: it depends on a partial unique
index and on the ordering of a real transaction against a real database, so
every test here runs the actual SQL.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.db import tenant_session
from app.modules.scheduling.providers.base import TimeSlot
from app.modules.scheduling.service import (
    SlotTakenError,
    book_appointment,
    find_available_slots,
)
from app.modules.tenants.models import Tenant
from tests.conftest import requires_services
from tests.fakes import FakeCalendarProvider

BUSINESS_HOURS = {
    "mon": [["09:00", "17:00"]],
    "tue": [["09:00", "17:00"]],
    "wed": [["09:00", "17:00"]],
    "thu": [["09:00", "17:00"]],
    "fri": [["09:00", "17:00"]],
}
# A Tuesday, far enough out that "now" never overtakes it.
SLOT_START = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


def _tenant(tenant_id: uuid.UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        slug=f"booking-{tenant_id.hex[:8]}",
        name="Booking Test Practice",
        vertical="dental",
        timezone="America/New_York",
        business_hours=BUSINESS_HOURS,
        settings={"scheduling": {"services": [{"name": "Cleaning", "duration_minutes": 60}]}},
        status="active",
    )


@pytest.fixture
def tenant():
    """A real tenant row, torn down afterwards (cascade removes appointments)."""
    tenant_id = uuid.uuid4()
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, business_hours) "
                "VALUES (:id, :slug, 'Booking Test', 'dental', 'America/New_York', "
                "CAST(:hours AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": f"booking-{tenant_id.hex[:8]}",
                "hours": __import__("json").dumps(BUSINESS_HOURS),
            },
        )
    try:
        yield _tenant(tenant_id)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


def _slot(start: datetime = SLOT_START, minutes: int = 60) -> TimeSlot:
    return TimeSlot(start=start, end=start + timedelta(minutes=minutes))


async def _confirmed_count(tenant_id: uuid.UUID, start: datetime) -> int:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM appointments "
                    "WHERE tenant_id = :t AND starts_at = :s AND status = 'confirmed'"
                ),
                {"t": str(tenant_id), "s": start},
            )
        ).scalar_one()


@requires_services
async def test_ten_concurrent_callers_produce_exactly_one_booking(tenant) -> None:
    """The guarantee. Ten callers race for one slot: one is booked, nine are
    told it's taken, and the calendar is written to exactly once."""
    calendar = FakeCalendarProvider()

    async def attempt(n: int):
        return await book_appointment(
            tenant=tenant,
            calendar=calendar,
            call_id=None,
            slot=_slot(),
            service="Cleaning",
            customer_name=f"Caller {n}",
            customer_phone=f"+1555000{n:04d}",
            customer_email=None,
            # Distinct keys: these are genuinely different callers, not a
            # retry of one booking. Only the unique index can separate them.
            idempotency_key=f"race-{tenant.id}-{n}",
        )

    results = await asyncio.gather(*(attempt(n) for n in range(10)), return_exceptions=True)

    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, SlotTakenError)]
    unexpected = [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, SlotTakenError)
    ]

    assert unexpected == [], f"unexpected failures: {unexpected}"
    assert len(winners) == 1, f"expected exactly one booking, got {len(winners)}"
    assert len(losers) == 9
    assert await _confirmed_count(tenant.id, SLOT_START) == 1
    # The losers must never have reached the vendor.
    assert len(calendar.booked) == 1


@requires_services
async def test_replayed_booking_returns_the_same_appointment(tenant) -> None:
    """A redelivered webhook must not create a second appointment, nor a
    second calendar event, nor report the slot as taken."""
    calendar = FakeCalendarProvider()
    kwargs = dict(
        tenant=tenant,
        calendar=calendar,
        call_id=None,
        slot=_slot(),
        service="Cleaning",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email=None,
        idempotency_key=f"replay-{tenant.id}",
    )

    first = await book_appointment(**kwargs)
    second = await book_appointment(**kwargs)

    assert first.appointment_id == second.appointment_id
    assert first.already_existed is False
    assert second.already_existed is True
    assert len(calendar.booked) == 1
    assert await _confirmed_count(tenant.id, SLOT_START) == 1


@requires_services
async def test_concurrent_replay_of_the_same_key_is_not_a_slot_conflict(tenant) -> None:
    """Two deliveries of the *same* booking arriving at once must both resolve
    to the one appointment — the idempotency lookup has to distinguish this
    from a genuine race, since both hit the same unique index."""
    calendar = FakeCalendarProvider()

    async def attempt():
        return await book_appointment(
            tenant=tenant,
            calendar=calendar,
            call_id=None,
            slot=_slot(),
            service="Cleaning",
            customer_name="Dana",
            customer_phone="+15550001111",
            customer_email=None,
            idempotency_key=f"same-key-{tenant.id}",
        )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results), results
    assert len({r.appointment_id for r in results}) == 1
    assert await _confirmed_count(tenant.id, SLOT_START) == 1


@requires_services
async def test_failed_calendar_write_releases_the_slot(tenant) -> None:
    """If the vendor write fails after we've reserved the row, the slot must
    become bookable again — otherwise a Google outage silently blocks the
    practice's calendar with appointments that don't exist anywhere."""
    failing = FakeCalendarProvider()
    failing.fail_booking = RuntimeError("google is down")

    with pytest.raises(RuntimeError):
        await book_appointment(
            tenant=tenant,
            calendar=failing,
            call_id=None,
            slot=_slot(),
            service="Cleaning",
            customer_name="Dana",
            customer_phone="+15550001111",
            customer_email=None,
            idempotency_key=f"fail-{tenant.id}",
        )

    assert await _confirmed_count(tenant.id, SLOT_START) == 0

    # ...and the next caller can take it.
    healthy = FakeCalendarProvider()
    outcome = await book_appointment(
        tenant=tenant,
        calendar=healthy,
        call_id=None,
        slot=_slot(),
        service="Cleaning",
        customer_name="Sam",
        customer_phone="+15550002222",
        customer_email=None,
        idempotency_key=f"after-fail-{tenant.id}",
    )
    assert outcome.already_existed is False
    assert await _confirmed_count(tenant.id, SLOT_START) == 1


@requires_services
async def test_cancelled_appointment_frees_the_slot(tenant) -> None:
    """The unique index is partial on status='confirmed', so cancelling must
    make the time immediately rebookable."""
    calendar = FakeCalendarProvider()
    first = await book_appointment(
        tenant=tenant,
        calendar=calendar,
        call_id=None,
        slot=_slot(),
        service="Cleaning",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email=None,
        idempotency_key=f"cancel-{tenant.id}",
    )
    async with tenant_session(tenant.id) as session:
        await session.execute(
            text(
                "UPDATE appointments SET status = 'cancelled', idempotency_key = NULL "
                "WHERE id = :id"
            ),
            {"id": str(first.appointment_id)},
        )

    second = await book_appointment(
        tenant=tenant,
        calendar=calendar,
        call_id=None,
        slot=_slot(),
        service="Cleaning",
        customer_name="Sam",
        customer_phone="+15550002222",
        customer_email=None,
        idempotency_key=f"cancel-rebook-{tenant.id}",
    )
    assert second.appointment_id != first.appointment_id
    assert await _confirmed_count(tenant.id, SLOT_START) == 1


@requires_services
async def test_availability_hides_our_own_confirmed_appointments(tenant) -> None:
    """A slot we booked ourselves must disappear from availability even when
    the calendar provider hasn't caught up — otherwise the next caller in the
    same minute is offered a slot that is already sold."""
    calendar = FakeCalendarProvider()
    window_start, window_end = NOW, NOW + timedelta(days=7)

    before = await find_available_slots(
        tenant=tenant,
        calendar=calendar,
        duration_minutes=60,
        window_start=window_start,
        window_end=window_end,
        now=NOW,
    )
    assert SLOT_START in {s.start for s in before}

    await book_appointment(
        tenant=tenant,
        calendar=calendar,
        call_id=None,
        slot=_slot(),
        service="Cleaning",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email=None,
        idempotency_key=f"hide-{tenant.id}",
    )

    after = await find_available_slots(
        tenant=tenant,
        calendar=calendar,
        duration_minutes=60,
        window_start=window_start,
        window_end=window_end,
        now=NOW,
    )
    assert SLOT_START not in {s.start for s in after}


@requires_services
async def test_availability_excludes_calendar_busy_periods(tenant) -> None:
    calendar = FakeCalendarProvider(
        busy=[TimeSlot(start=SLOT_START, end=SLOT_START + timedelta(minutes=60))]
    )
    slots = await find_available_slots(
        tenant=tenant,
        calendar=calendar,
        duration_minutes=60,
        window_start=NOW,
        window_end=NOW + timedelta(days=7),
        now=NOW,
    )
    assert SLOT_START not in {s.start for s in slots}
