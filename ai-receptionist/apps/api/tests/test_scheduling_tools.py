"""The two in-call scheduling tools.

Everything these return is spoken to a caller, so the tests assert on what the
receptionist actually says — especially in the failure cases, where the
requirement is that a calendar outage sounds like a receptionist taking a
message rather than like an error.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.modules.scheduling.providers.base import TimeSlot
from app.modules.scheduling.tools import (
    COULD_NOT_CHECK,
    NOTHING_AVAILABLE,
    NUMBER_NEEDED,
    NUMBER_UNCLEAR,
    handle_book_appointment,
    handle_check_availability,
    parse_when,
    resolve_duration,
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
SETTINGS = {
    "scheduling": {
        "services": [
            {"name": "Cleaning & check-up", "duration_minutes": 60},
            {"name": "Emergency exam", "duration_minutes": 30},
        ],
        "min_notice_hours": 2,
    }
}
# Monday 2026-09-14, 12:00 UTC = 8am New York, before opening.
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _tenant(tenant_id: uuid.UUID | None = None) -> Tenant:
    tenant_id = tenant_id or uuid.uuid4()
    return Tenant(
        id=tenant_id,
        slug=f"tools-{tenant_id.hex[:8]}",
        name="Bright Smile Dental",
        vertical="dental",
        timezone="America/New_York",
        business_hours=BUSINESS_HOURS,
        settings=SETTINGS,
        status="active",
    )


# ── pure helpers ───────────────────────────────────────────────────────────


def test_service_duration_matches_loosely() -> None:
    """Callers say 'a cleaning', not 'Cleaning & check-up'. Refusing to match
    would silently book the wrong length of appointment."""
    tenant = _tenant()
    assert resolve_duration(tenant, "cleaning") == 60
    assert resolve_duration(tenant, "Cleaning & check-up") == 60
    assert resolve_duration(tenant, "emergency exam") == 30


def test_unknown_service_falls_back_rather_than_failing() -> None:
    tenant = _tenant()
    assert resolve_duration(tenant, "teeth whitening consultation") == 60
    assert resolve_duration(tenant, "") == 60


def test_service_duration_default_when_tenant_configures_none() -> None:
    tenant = _tenant()
    tenant.settings = {}
    assert resolve_duration(tenant, "anything") == 30


def test_naive_time_is_interpreted_in_the_practices_timezone() -> None:
    """The model is told the local time, so a bare '2026-09-15T09:00' means
    9am for the practice. Reading it as UTC would book 5am local."""
    parsed = parse_when("2026-09-15T09:00", "America/New_York")
    assert parsed == datetime(2026, 9, 15, 13, 0, tzinfo=UTC)


def test_explicit_offset_is_respected() -> None:
    parsed = parse_when("2026-09-15T09:00-07:00", "America/New_York")
    assert parsed == datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


def test_unparseable_time_is_none_not_an_exception() -> None:
    assert parse_when("sometime next week", "America/New_York") is None
    assert parse_when(None, "America/New_York") is None


# ── check_availability ─────────────────────────────────────────────────────


@requires_services
async def test_availability_is_offered_in_spoken_form() -> None:
    result = await handle_check_availability(
        tenant=_tenant(),
        calendar=FakeCalendarProvider(),
        service="cleaning",
        preferred_time="2026-09-15T09:05",  # between slots: offer the nearest
        now=NOW,
    )
    assert result.startswith("I have tomorrow at ")  # NOW is Monday; Tuesday is tomorrow
    assert "9 am" in result
    assert "2026" not in result  # no ISO timestamps read aloud
    assert result.endswith("Which works best for you?")


@requires_services
async def test_an_open_requested_time_is_simply_confirmed() -> None:
    """If the time they asked for is free, say yes, without making the caller
    listen to (and remember) a list of other times."""
    result = await handle_check_availability(
        tenant=_tenant(),
        calendar=FakeCalendarProvider(),
        service="cleaning",
        preferred_time="2026-09-15T09:00",
        now=NOW,
    )
    assert result == "Yes, tomorrow at 9 am is open. Shall I book it?"


@requires_services
async def test_availability_widens_when_the_preferred_day_is_full() -> None:
    """Offering nothing because the caller's guessed day is booked would lose
    the appointment; the search widens instead."""
    busy_all_tuesday = [
        TimeSlot(
            start=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
            end=datetime(2026, 9, 15, 22, 0, tzinfo=UTC),
        )
    ]
    result = await handle_check_availability(
        tenant=_tenant(),
        calendar=FakeCalendarProvider(busy=busy_all_tuesday),
        service="cleaning",
        preferred_time="2026-09-15T09:00",
        now=NOW,
    )
    assert result != NOTHING_AVAILABLE
    # Tuesday is full, so nothing on Tuesday may be offered — but the caller
    # still gets real times rather than being turned away.
    assert "tomorrow" not in result
    assert "today" in result  # NOW is Monday morning: the nearest open times


@requires_services
async def test_calendar_outage_sounds_like_a_receptionist() -> None:
    """Risk R9: a vendor failure must degrade to taking a message, never to
    an error or to silence."""
    calendar = FakeCalendarProvider()
    calendar.fail_busy = RuntimeError("google timed out")

    result = await handle_check_availability(
        tenant=_tenant(), calendar=calendar, service="cleaning", preferred_time=None, now=NOW
    )
    assert result == COULD_NOT_CHECK
    assert "call you back" in result


@requires_services
async def test_fully_booked_horizon_offers_a_callback() -> None:
    busy = [TimeSlot(start=NOW, end=NOW + timedelta(days=30))]
    result = await handle_check_availability(
        tenant=_tenant(),
        calendar=FakeCalendarProvider(busy=busy),
        service="cleaning",
        preferred_time=None,
        now=NOW,
    )
    assert result == NOTHING_AVAILABLE


# ── book_appointment ───────────────────────────────────────────────────────


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


@pytest.fixture
def booking_tenant():
    tenant_id = uuid.uuid4()
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, business_hours, "
                "settings) VALUES (:id, :slug, 'Bright Smile Dental', 'dental', "
                "'America/New_York', CAST(:hours AS jsonb), CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": f"tools-{tenant_id.hex[:8]}",
                "hours": json.dumps(BUSINESS_HOURS),
                "settings": json.dumps(SETTINGS),
            },
        )
    try:
        yield _tenant(tenant_id)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


class FakeRedis:
    """Captures enqueued jobs — the confirmation must be queued, never sent
    on the call path."""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []

    async def rpush(self, queue: str, payload: str) -> int:
        self.pushed.append((queue, payload))
        return len(self.pushed)


@requires_services
async def test_successful_booking_confirms_and_queues_messages(booking_tenant) -> None:
    redis = FakeRedis()
    result = await handle_book_appointment(
        tenant=booking_tenant,
        calendar=FakeCalendarProvider(),
        redis=redis,
        call_id=None,
        service="cleaning",
        starts_at="2026-09-15T09:00",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email="dana@example.com",
        now=NOW,
    )
    # Read back with the date (a week-later Tuesday must not sound like this
    # one), and say where the confirmation goes.
    assert result.startswith("You're all set: cleaning, tomorrow, September 15th at 9 am.")
    assert "text a confirmation to the number you're calling from, and email it" in result
    assert len(redis.pushed) == 1
    queue, payload = redis.pushed[0]
    assert queue == "queue:confirmations"
    assert json.loads(payload)["type"] == "appointment_confirmation"


@requires_services
async def test_booking_without_email_promises_only_a_text(booking_tenant) -> None:
    result = await handle_book_appointment(
        tenant=booking_tenant,
        calendar=FakeCalendarProvider(),
        redis=FakeRedis(),
        call_id=None,
        service="cleaning",
        starts_at="2026-09-15T09:00",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email=None,
        now=NOW,
    )
    assert result.endswith("I'll text a confirmation to the number you're calling from.")
    assert "email" not in result


@requires_services
async def test_losing_the_race_offers_alternatives(booking_tenant) -> None:
    """Two callers, same slot. The second must be told plainly and given real
    alternatives rather than an apology with nothing behind it."""
    calendar = FakeCalendarProvider()
    common = dict(
        tenant=booking_tenant,
        calendar=calendar,
        redis=FakeRedis(),
        service="cleaning",
        starts_at="2026-09-15T09:00",
        customer_phone="+15550001111",
        customer_email=None,
        now=NOW,
    )
    first = await handle_book_appointment(call_id=None, customer_name="Dana", **common)
    second = await handle_book_appointment(call_id=None, customer_name="Sam", **common)

    assert "You're all set" in first
    assert "just taken" in second
    assert "Would any of those work?" in second
    # Real alternatives, and never the slot that was just lost.
    assert "Tuesday at 9 am" not in second
    assert " at " in second.split("I do have", 1)[1]


@requires_services
async def test_unparseable_time_asks_the_caller_to_repeat(booking_tenant) -> None:
    result = await handle_book_appointment(
        tenant=booking_tenant,
        calendar=FakeCalendarProvider(),
        redis=FakeRedis(),
        call_id=None,
        service="cleaning",
        starts_at="whenever suits you",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email=None,
        now=NOW,
    )
    assert "could you say that again" in result


@requires_services
async def test_calendar_failure_during_booking_takes_a_message(booking_tenant) -> None:
    calendar = FakeCalendarProvider()
    calendar.fail_booking = RuntimeError("google is down")
    redis = FakeRedis()

    result = await handle_book_appointment(
        tenant=booking_tenant,
        calendar=calendar,
        redis=redis,
        call_id=None,
        service="cleaning",
        starts_at="2026-09-15T09:00",
        customer_name="Dana",
        customer_phone="+15550001111",
        customer_email=None,
        now=NOW,
    )
    assert result == COULD_NOT_CHECK
    # Nothing was booked, so nothing may be confirmed to the customer.
    assert redis.pushed == []


# ── the confirmation number ─────────────────────────────────────────────


async def _book(tenant, redis, *, caller_id="+15550001111", **extra):
    return await handle_book_appointment(
        tenant=tenant,
        calendar=FakeCalendarProvider(),
        redis=redis,
        call_id=None,
        service="cleaning",
        starts_at="2026-09-15T09:00",
        customer_name="Dana",
        customer_phone=caller_id,
        customer_email=None,
        now=NOW,
        **extra,
    )


def _stored_phones(tenant_id) -> list[str]:
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        rows = conn.execute(
            text("SELECT customer_phone FROM appointments WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).all()
    engine.dispose()
    return [r[0] for r in rows]


@requires_services
async def test_a_different_number_is_stored_and_read_back_digit_by_digit(booking_tenant) -> None:
    result = await _book(booking_tenant, FakeRedis(), callback_phone="(303) 555-0199")
    assert result.endswith("I'll text a confirmation to 3 0 3, 5 5 5, 0 1 9 9.")
    assert _stored_phones(booking_tenant.id) == ["+13035550199"]  # what the texts go to


@requires_services
async def test_giving_the_number_you_are_calling_from_reads_as_such(booking_tenant) -> None:
    result = await _book(
        booking_tenant, FakeRedis(), caller_id="+13035550199", callback_phone="303 555 0199"
    )
    assert result.endswith("to the number you're calling from.")


@requires_services
async def test_a_misheard_number_books_nothing_and_asks_again(booking_tenant) -> None:
    redis = FakeRedis()
    result = await _book(booking_tenant, redis, callback_phone="303-555-019")
    assert result == NUMBER_UNCLEAR
    assert _stored_phones(booking_tenant.id) == [] and redis.pushed == []


@requires_services
async def test_a_blocked_caller_id_is_asked_for_a_number_first(booking_tenant) -> None:
    redis = FakeRedis()
    assert await _book(booking_tenant, redis, caller_id="") == NUMBER_NEEDED
    assert _stored_phones(booking_tenant.id) == [] and redis.pushed == []
    # ...and the same booking goes through once they give one.
    result = await _book(booking_tenant, redis, caller_id="", callback_phone="303-555-0199")
    assert result.startswith("You're all set")
    assert _stored_phones(booking_tenant.id) == ["+13035550199"]


@requires_services
async def test_no_text_is_promised_when_the_business_sends_none(booking_tenant) -> None:
    booking_tenant.settings = {
        **booking_tenant.settings,
        "notifications": {"sms_confirmation": False, "email_confirmation": True},
    }
    result = await _book(booking_tenant, FakeRedis())
    assert "text" not in result
    assert result.endswith("9 am.")
