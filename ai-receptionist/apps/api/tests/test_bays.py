"""Service bays: a business that can run several appointments at once.

An auto shop with three bays can take three oil changes at 9:00 and must turn
away a fourth; a single-chair practice can take one. The availability maths is
pure and tested exhaustively below; the booking guarantees are tested against
real Postgres, because only the database constraint makes them hold under a
genuine race.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.db import tenant_session
from app.modules.scheduling.availability import available_slots, free_bay
from app.modules.scheduling.providers.base import TimeSlot
from app.modules.scheduling.service import SlotTakenError, bay_count, book_appointment
from app.modules.tenants.models import Tenant
from tests.conftest import requires_services
from tests.fakes import FakeCalendarProvider

HOURS = {d: [["08:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)  # Monday, 8am New York
NINE = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)  # Tuesday, 9am New York


def _slot(start: datetime, minutes: int) -> TimeSlot:
    return TimeSlot(start=start, end=start + timedelta(minutes=minutes))


def _open_at_nine(*, bays: int, bay_bookings=None, busy=None, minutes: int = 60) -> bool:
    slots = available_slots(
        business_hours=HOURS,
        timezone="America/New_York",
        window_start=NINE - timedelta(hours=1),
        window_end=NINE + timedelta(hours=3),
        duration_minutes=minutes,
        busy_periods=busy or [],
        now=NOW,
        bay_bookings=bay_bookings,
        bays=bays,
    )
    return any(s.start == NINE for s in slots)


# ── the maths ────────────────────────────────────────────────────────────


def test_a_slot_stays_open_while_any_bay_is_free() -> None:
    two_booked = {1: [_slot(NINE, 60)], 2: [_slot(NINE, 60)]}
    assert _open_at_nine(bays=3, bay_bookings=two_booked)
    three_booked = {**two_booked, 3: [_slot(NINE, 60)]}
    assert not _open_at_nine(bays=3, bay_bookings=three_booked)


def test_a_partial_overlap_in_every_bay_closes_the_slot() -> None:
    """Each bay is busy for only part of 9:00-10:00, but no bay is free for
    all of it — the hour-long job has nowhere to go."""
    staggered = {
        1: [_slot(NINE - timedelta(minutes=30), 45)],
        2: [_slot(NINE + timedelta(minutes=45), 30)],
    }
    assert not _open_at_nine(bays=2, bay_bookings=staggered)


def test_calendar_busy_time_blocks_every_bay() -> None:
    """A closure or staff block on the calendar applies to the whole shop."""
    assert not _open_at_nine(bays=3, bay_bookings={}, busy=[_slot(NINE, 120)])


def test_single_bay_behaviour_is_unchanged() -> None:
    assert _open_at_nine(bays=1, bay_bookings={})
    assert not _open_at_nine(bays=1, bay_bookings={1: [_slot(NINE, 30)]})


def test_free_bay_fills_the_lowest_bay_first() -> None:
    bookings = {1: [_slot(NINE, 60)], 3: [_slot(NINE, 60)]}
    assert free_bay(_slot(NINE, 60), bookings, 3) == 2
    assert free_bay(_slot(NINE + timedelta(hours=1), 60), bookings, 3) == 1  # back-to-back is fine
    assert free_bay(_slot(NINE, 60), {**bookings, 2: [_slot(NINE, 60)]}, 3) is None


@pytest.mark.parametrize(
    ("configured", "expected"), [(None, 1), (3, 3), ("4", 4), (0, 1), ("x", 1)]
)
def test_bay_count_defaults_to_one_and_tolerates_bad_config(configured, expected) -> None:
    scheduling = {} if configured is None else {"bays": configured}
    tenant = Tenant(settings={"scheduling": scheduling})
    assert bay_count(tenant) == expected


# ── the guarantees, against Postgres ─────────────────────────────────────


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


def _make_tenant(bays: int):
    tenant_id = uuid.uuid4()
    settings = {
        "scheduling": {"bays": bays, "services": [{"name": "Oil change", "duration_minutes": 45}]}
    }
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants "
                "(id, slug, name, vertical, timezone, business_hours, settings) "
                "VALUES (:id, :slug, 'Bay Test Garage', 'auto_repair', 'America/New_York', "
                "CAST(:hours AS jsonb), CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": f"bays-{tenant_id.hex[:8]}",
                "hours": json.dumps(HOURS),
                "settings": json.dumps(settings),
            },
        )
    tenant = Tenant(
        id=tenant_id,
        slug=f"bays-{tenant_id.hex[:8]}",
        name="Bay Test Garage",
        vertical="auto_repair",
        timezone="America/New_York",
        business_hours=HOURS,
        settings=settings,
        status="active",
    )
    return tenant, engine


@pytest.fixture
def three_bay_shop():
    tenant, engine = _make_tenant(3)
    try:
        yield tenant
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant.id)})
        engine.dispose()


@pytest.fixture
def one_bay_practice():
    tenant, engine = _make_tenant(1)
    try:
        yield tenant
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant.id)})
        engine.dispose()


async def _book(tenant, calendar, n: int, start: datetime = NINE, minutes: int = 45, **extra):
    return await book_appointment(
        tenant=tenant,
        calendar=calendar,
        call_id=None,
        slot=_slot(start, minutes),
        service="Oil change",
        customer_name=f"Driver {n}",
        customer_phone=f"+1555000{n:04d}",
        customer_email=None,
        idempotency_key=f"bays-{tenant.id}-{n}",
        **extra,
    )


async def _bays_booked(tenant_id: uuid.UUID) -> list[int]:
    async with tenant_session(tenant_id) as session:
        rows = await session.execute(
            text(
                "SELECT bay FROM appointments "
                "WHERE tenant_id = :t AND status = 'confirmed' ORDER BY bay"
            ),
            {"t": str(tenant_id)},
        )
        return [r[0] for r in rows]


@requires_services
async def test_four_racing_callers_fill_three_bays_and_one_is_turned_away(three_bay_shop) -> None:
    calendar = FakeCalendarProvider()
    results = await asyncio.gather(
        *(_book(three_bay_shop, calendar, n) for n in range(4)), return_exceptions=True
    )

    unexpected = [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, SlotTakenError)
    ]
    assert unexpected == [], unexpected
    assert sum(isinstance(r, SlotTakenError) for r in results) == 1
    assert await _bays_booked(three_bay_shop.id) == [1, 2, 3]
    assert len(calendar.booked) == 3, "the turned-away caller must never reach the calendar"


@requires_services
async def test_multi_bay_calendar_events_do_not_block_the_shop(three_bay_shop) -> None:
    """Otherwise the first booking would make Google's free/busy report the
    whole shop busy, and bays two and three could never be offered."""
    calendar = FakeCalendarProvider()
    await _book(three_bay_shop, calendar, 1)
    assert calendar.booked[0].blocks_time is False
    assert calendar.booked[0].summary.startswith("Bay 1 · ")


@requires_services
async def test_single_bay_calendar_events_still_block_time(one_bay_practice) -> None:
    calendar = FakeCalendarProvider()
    await _book(one_bay_practice, calendar, 1)
    assert calendar.booked[0].blocks_time is True
    assert not calendar.booked[0].summary.startswith("Bay")


@requires_services
async def test_overlapping_bookings_are_refused_not_only_same_start_ones(one_bay_practice) -> None:
    """The old guard was a unique index on start time, so 9:00-9:45 and
    9:30-10:15 could both be confirmed. The exclusion constraint refuses it."""
    calendar = FakeCalendarProvider()
    await _book(one_bay_practice, calendar, 1, start=NINE, minutes=45)
    with pytest.raises(SlotTakenError):
        await _book(one_bay_practice, calendar, 2, start=NINE + timedelta(minutes=30), minutes=45)
    # Back-to-back is fine: half-open intervals.
    await _book(one_bay_practice, calendar, 3, start=NINE + timedelta(minutes=45), minutes=45)


@requires_services
async def test_booking_details_are_stored_on_the_appointment(three_bay_shop) -> None:
    details = {
        "vehicle": "2017 Subaru Outback",
        "mileage": "84,000",
        "concern": "squeal when braking",
    }
    outcome = await _book(three_bay_shop, FakeCalendarProvider(), 1, details=details)
    async with tenant_session(three_bay_shop.id) as session:
        stored = (
            await session.execute(
                text("SELECT details FROM appointments WHERE id = :id"),
                {"id": str(outcome.appointment_id)},
            )
        ).scalar_one()
    assert stored == details


@requires_services
async def test_a_retried_booking_replays_instead_of_taking_a_second_bay(three_bay_shop) -> None:
    calendar = FakeCalendarProvider()
    first, second = await asyncio.gather(
        _book(three_bay_shop, calendar, 7), _book(three_bay_shop, calendar, 7)
    )
    assert first.appointment_id == second.appointment_id
    assert await _bays_booked(three_bay_shop.id) == [1]


@requires_services
@pytest.mark.parametrize("send_confirmations", [True, False])
async def test_the_booking_tool_saves_vehicle_details_and_respects_confirmations(
    three_bay_shop, send_confirmations
) -> None:
    """End to end through the tool the voice agent calls: the tenant's
    booking_fields arrive as tool arguments and land on the appointment, and a
    test-lab booking (send_confirmations=False) queues no text or email."""
    from app.modules.conversation.tools import ToolExecutor
    from tests.fakes import FakeEmbeddingProvider, FakeLLMProvider
    from tests.test_scheduling_tools import FakeRedis
    from tests.test_tool_dispatch import _agent_config

    shop = three_bay_shop
    shop.settings = {
        **shop.settings,
        "scheduling": {
            **shop.settings["scheduling"],
            "booking_fields": [{"key": "vehicle", "description": "Vehicle"}],
            "confirm_detail": "vehicle",
        },
    }
    redis = FakeRedis()
    executor = ToolExecutor(
        FakeLLMProvider(),
        FakeEmbeddingProvider(),
        calendar=FakeCalendarProvider(),
        redis=redis,
        send_confirmations=send_confirmations,
    )
    start_local = (datetime.now(UTC) + timedelta(days=14)).astimezone()
    while start_local.weekday() >= 5:
        start_local += timedelta(days=1)
    spoken = await executor.dispatch(
        tenant=shop,
        call_id=None,
        caller_e164="+15550009999",
        agent_config=_agent_config(),
        tool_name="book_appointment",
        arguments={
            "service": "Oil change",
            "starts_at": start_local.strftime("%Y-%m-%dT10:00"),
            "customer_name": "Priya",
            "vehicle": "2019 Toyota RAV4",
            "not_a_field": "ignored",
        },
    )
    assert "all set" in spoken.lower(), spoken
    # The caller hears what was booked, including the detail most worth
    # checking, so a misheard vehicle is caught before they hang up.
    assert "oil change for your 2019 Toyota RAV4" in spoken, spoken
    assert (len(redis.pushed) == 1) is send_confirmations
    assert ("text" in spoken) is send_confirmations

    async with tenant_session(shop.id) as session:
        details = (
            await session.execute(
                text("SELECT details FROM appointments WHERE tenant_id = :t"), {"t": str(shop.id)}
            )
        ).scalar_one()
    assert details == {"vehicle": "2019 Toyota RAV4"}
