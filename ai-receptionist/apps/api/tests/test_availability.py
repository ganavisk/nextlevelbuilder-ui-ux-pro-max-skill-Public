"""Availability maths. No database, no calendar — these are the rules that
decide what a caller is offered, and the ones that break subtly in
production: timezone handling, closing-time boundaries, and DST.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.modules.scheduling.availability import (
    available_slots,
    describe_slots,
    generate_candidate_slots,
    overlaps,
    pick_offers,
    subtract_busy,
)
from app.modules.scheduling.providers.base import TimeSlot

NY = ZoneInfo("America/New_York")
WEEKDAY_9_TO_5 = {
    "mon": [["09:00", "17:00"]],
    "tue": [["09:00", "17:00"]],
    "wed": [["09:00", "17:00"]],
    "thu": [["09:00", "17:00"]],
    "fri": [["09:00", "17:00"]],
}


def _ny(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(UTC)


def test_slots_fall_inside_local_business_hours_not_utc_hours() -> None:
    """The classic bug: treating '09:00' as UTC. In New York in September
    that is 5am local — five hours before the practice opens."""
    # Tuesday 2026-09-15.
    slots = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 9, 15, 0),
        window_end=_ny(2026, 9, 16, 0),
        duration_minutes=60,
        now=_ny(2026, 9, 14, 12),
    )
    assert slots
    local_hours = {s.start.astimezone(NY).hour for s in slots}
    assert min(local_hours) == 9
    # A 60-minute slot cannot start at 5pm; the last valid start is 4pm.
    assert max(local_hours) == 16


def test_slot_must_fit_entirely_before_closing() -> None:
    """A 90-minute appointment at 4pm would run to 5:30pm — past close — and
    must not be offered even though 4pm itself is within opening hours."""
    slots = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 9, 15, 0),
        window_end=_ny(2026, 9, 16, 0),
        duration_minutes=90,
        now=_ny(2026, 9, 14, 12),
    )
    latest = max(s.end for s in slots)
    assert latest == _ny(2026, 9, 15, 17)
    assert all(s.end <= _ny(2026, 9, 15, 17) for s in slots)


def test_closed_days_produce_no_slots() -> None:
    # 2026-09-19 is a Saturday; WEEKDAY_9_TO_5 has no weekend entry.
    slots = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 9, 19, 0),
        window_end=_ny(2026, 9, 20, 0),
        duration_minutes=30,
        now=_ny(2026, 9, 18, 12),
    )
    assert slots == []


def test_minimum_notice_excludes_imminent_slots() -> None:
    """A caller at 9:05am must not be offered 9:15am when the practice needs
    two hours' notice."""
    now = _ny(2026, 9, 15, 9, 5)
    slots = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=now,
        window_end=_ny(2026, 9, 16, 0),
        duration_minutes=30,
        now=now,
        min_notice_hours=2,
    )
    assert slots
    assert min(s.start for s in slots) >= now + timedelta(hours=2)


def test_slots_never_start_in_the_past() -> None:
    now = _ny(2026, 9, 15, 13, 37)
    slots = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 9, 15, 0),
        window_end=_ny(2026, 9, 16, 0),
        duration_minutes=30,
        now=now,
    )
    assert all(s.start >= now for s in slots)


def test_dst_transition_keeps_local_opening_hours() -> None:
    """US DST ends Sunday 2026-11-01. Monday the 2nd is UTC-5 where Friday
    the 30th was UTC-4 — the practice still opens at 9am local both days,
    which is a different UTC hour. Anything that stores the offset rather
    than the zone gets this wrong."""
    before = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 10, 30, 0),
        window_end=_ny(2026, 10, 31, 0),
        duration_minutes=60,
        now=_ny(2026, 10, 29, 12),
    )
    after = generate_candidate_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 11, 2, 0),
        window_end=_ny(2026, 11, 3, 0),
        duration_minutes=60,
        now=_ny(2026, 11, 1, 12),
    )
    assert min(s.start.astimezone(NY).hour for s in before) == 9
    assert min(s.start.astimezone(NY).hour for s in after) == 9
    # ...and the UTC hours genuinely differ, so the test isn't vacuous.
    assert min(s.start.hour for s in before) != min(s.start.hour for s in after)


def test_back_to_back_appointments_remain_bookable() -> None:
    """Half-open intervals: a slot ending exactly when a busy period begins
    is not a conflict. Treating it as one loses an appointment per boundary."""
    slot = TimeSlot(start=_ny(2026, 9, 15, 9), end=_ny(2026, 9, 15, 10))
    adjacent_after = TimeSlot(start=_ny(2026, 9, 15, 10), end=_ny(2026, 9, 15, 11))
    adjacent_before = TimeSlot(start=_ny(2026, 9, 15, 8), end=_ny(2026, 9, 15, 9))
    genuine_overlap = TimeSlot(start=_ny(2026, 9, 15, 9, 30), end=_ny(2026, 9, 15, 10, 30))

    assert not overlaps(slot, adjacent_after)
    assert not overlaps(slot, adjacent_before)
    assert overlaps(slot, genuine_overlap)


def test_busy_periods_remove_only_conflicting_slots() -> None:
    slots = [
        TimeSlot(start=_ny(2026, 9, 15, 9), end=_ny(2026, 9, 15, 10)),
        TimeSlot(start=_ny(2026, 9, 15, 10), end=_ny(2026, 9, 15, 11)),
        TimeSlot(start=_ny(2026, 9, 15, 11), end=_ny(2026, 9, 15, 12)),
    ]
    busy = [TimeSlot(start=_ny(2026, 9, 15, 10, 15), end=_ny(2026, 9, 15, 10, 45))]
    remaining = subtract_busy(slots, busy)
    assert [s.start for s in remaining] == [_ny(2026, 9, 15, 9), _ny(2026, 9, 15, 11)]


def test_available_slots_excludes_calendar_conflicts() -> None:
    busy = [TimeSlot(start=_ny(2026, 9, 15, 9), end=_ny(2026, 9, 15, 12))]
    slots = available_slots(
        business_hours=WEEKDAY_9_TO_5,
        timezone="America/New_York",
        window_start=_ny(2026, 9, 15, 0),
        window_end=_ny(2026, 9, 16, 0),
        duration_minutes=60,
        busy_periods=busy,
        now=_ny(2026, 9, 14, 12),
    )
    assert slots
    assert min(s.start.astimezone(NY).hour for s in slots) == 12


# Monday, September 14th 2026, 10 am in New York.
MONDAY = _ny(2026, 9, 14, 10)


def _slot(day, hour, minute=0, minutes=60) -> TimeSlot:
    start = _ny(2026, 9, day, hour, minute)
    return TimeSlot(start=start, end=start + timedelta(minutes=minutes))


def test_describe_slots_is_speakable() -> None:
    """Read aloud by the voice agent: no ISO timestamps, no 24-hour clock,
    each day named once, and noon said as noon."""
    slots = [_slot(15, 9), _slot(15, 14, 30), _slot(16, 12)]
    spoken = describe_slots(slots, "America/New_York", limit=3, now=MONDAY)
    assert spoken == "tomorrow at 9 am or 2:30 pm, or Wednesday at noon"


def test_one_day_is_named_once() -> None:
    slots = [_slot(15, 7, 30), _slot(15, 10, 15), _slot(15, 13)]
    spoken = describe_slots(slots, "America/New_York", now=MONDAY)
    assert spoken == "tomorrow at 7:30 am, 10:15 am, or 1 pm"


def test_days_are_named_relative_to_today() -> None:
    def day_of(day: int) -> str:
        return describe_slots([_slot(day, 9)], "America/New_York", now=MONDAY)

    assert day_of(14) == "today at 9 am"
    assert day_of(15) == "tomorrow at 9 am"
    assert day_of(18) == "Friday at 9 am"
    assert day_of(20) == "Sunday at 9 am"  # six days out: still this week


def test_a_weekday_a_week_or_more_away_carries_its_date() -> None:
    """Found in the Test Lab: on a Tuesday, a booking for the *next* Tuesday
    was confirmed as just "Tuesday", which a caller hears as today."""
    tuesday = _ny(2026, 9, 15, 10)
    spoken = describe_slots([_slot(22, 10)], "America/New_York", now=tuesday)
    assert spoken == "Tuesday, September 22nd at 10 am"


def test_today_is_judged_in_the_business_timezone() -> None:
    """11 pm Monday in New York is already Tuesday in UTC; Tuesday's slot is
    still "tomorrow" to the caller."""
    late_monday = _ny(2026, 9, 14, 23)
    assert describe_slots([_slot(15, 9)], "America/New_York", now=late_monday) == (
        "tomorrow at 9 am"
    )


def test_a_confirmation_always_carries_the_date() -> None:
    def confirm(day: int) -> str:
        return describe_slots([_slot(day, 9)], "America/New_York", now=MONDAY, with_date=True)

    assert confirm(14) == "today, September 14th at 9 am"
    assert confirm(15) == "tomorrow, September 15th at 9 am"
    assert confirm(18) == "Friday, September 18th at 9 am"
    assert confirm(21) == "Monday, September 21st at 9 am"


def test_describe_slots_single_slot_has_no_conjunction() -> None:
    assert describe_slots([_slot(15, 9)], "America/New_York", now=MONDAY) == "tomorrow at 9 am"


def test_describe_slots_empty_is_empty_string() -> None:
    assert describe_slots([], "America/New_York", now=MONDAY) == ""


def _every_15_minutes(day: int, from_hour: int, to_hour: int) -> list[TimeSlot]:
    start = _ny(2026, 9, day, from_hour)
    count = (to_hour - from_hour) * 4
    return [
        TimeSlot(
            start=start + timedelta(minutes=15 * i), end=start + timedelta(minutes=15 * i + 45)
        )
        for i in range(count)
    ]


def test_offers_are_spread_out_not_back_to_back() -> None:
    """Found in the Test Lab: "7:30, 7:45, or 8" is one choice said three
    times. Offers are at least 90 minutes apart."""
    offers = pick_offers(_every_15_minutes(15, 8, 17), 3)
    starts = [o.start.astimezone(NY).strftime("%H:%M") for o in offers]
    assert starts == ["08:00", "09:30", "11:00"]


def test_offers_centre_on_the_time_the_caller_asked_for() -> None:
    offers = pick_offers(_every_15_minutes(15, 8, 17), 3, preferred=_ny(2026, 9, 15, 14, 5))
    starts = [o.start.astimezone(NY).strftime("%H:%M") for o in offers]
    assert starts[1] == "14:00"  # nearest to 2:05, spoken in time order
    assert starts == ["12:30", "14:00", "15:30"]


def test_a_nearly_full_day_still_fills_the_offers() -> None:
    slots = _every_15_minutes(15, 9, 10)  # one free hour
    offers = pick_offers(slots, 3)
    assert len(offers) == 3
    assert offers == sorted(offers, key=lambda o: o.start)


def test_zero_duration_and_inverted_window_are_rejected() -> None:
    common = {
        "business_hours": WEEKDAY_9_TO_5,
        "timezone": "America/New_York",
        "now": _ny(2026, 9, 14, 12),
    }
    assert (
        generate_candidate_slots(
            window_start=_ny(2026, 9, 15, 0),
            window_end=_ny(2026, 9, 16, 0),
            duration_minutes=0,
            **common,
        )
        == []
    )
    assert (
        generate_candidate_slots(
            window_start=_ny(2026, 9, 16, 0),
            window_end=_ny(2026, 9, 15, 0),
            duration_minutes=30,
            **common,
        )
        == []
    )
