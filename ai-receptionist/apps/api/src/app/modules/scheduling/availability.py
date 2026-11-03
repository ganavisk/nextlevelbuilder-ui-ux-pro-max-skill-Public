"""Availability maths: turn a tenant's business hours into bookable slots.

Deliberately pure — no database, no calendar, no clock of its own. Everything
it needs is passed in, which is what makes the timezone and boundary rules
(the parts that actually break in production) cheap to test exhaustively.

Business hours are local to the tenant's timezone; slots are computed in that
local frame and returned as UTC-aware datetimes so every downstream
comparison is unambiguous.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.modules.scheduling.providers.base import TimeSlot

# Order matters: index matches datetime.weekday() (Monday == 0).
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULT_STEP_MINUTES = 15


def _parse_hhmm(value: str) -> tuple[int, int]:
    hours, _, minutes = value.partition(":")
    return int(hours), int(minutes)


def generate_candidate_slots(
    *,
    business_hours: dict[str, list[list[str]]],
    timezone: str,
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    now: datetime,
    min_notice_hours: int = 0,
    step_minutes: int = DEFAULT_STEP_MINUTES,
) -> list[TimeSlot]:
    """Every slot of `duration_minutes` that fits entirely inside the tenant's
    opening hours, starts on a `step_minutes` boundary, and respects the
    minimum notice period.

    A slot must fit *entirely* within an opening interval — an appointment
    that would run past closing is not offered.
    """
    if duration_minutes <= 0 or window_end <= window_start:
        return []

    tz = ZoneInfo(timezone)
    duration = timedelta(minutes=duration_minutes)
    earliest = max(window_start, now + timedelta(hours=min_notice_hours))

    slots: list[TimeSlot] = []
    # Walk local calendar days, not UTC days: an 8am local opening belongs to
    # its local date regardless of which UTC day that falls on.
    day = window_start.astimezone(tz).date()
    last_day = window_end.astimezone(tz).date()

    while day <= last_day:
        for opening in business_hours.get(WEEKDAY_KEYS[day.weekday()], []):
            if len(opening) != 2:
                continue
            open_h, open_m = _parse_hhmm(opening[0])
            close_h, close_m = _parse_hhmm(opening[1])
            opens = datetime(day.year, day.month, day.day, open_h, open_m, tzinfo=tz)
            closes = datetime(day.year, day.month, day.day, close_h, close_m, tzinfo=tz)

            cursor = opens
            while cursor + duration <= closes:
                start = cursor.astimezone(UTC)
                end = (cursor + duration).astimezone(UTC)
                if start >= earliest and end <= window_end:
                    slots.append(TimeSlot(start=start, end=end))
                cursor += timedelta(minutes=step_minutes)
        day += timedelta(days=1)

    return slots


def overlaps(slot: TimeSlot, busy: TimeSlot) -> bool:
    """Half-open intervals: a slot ending exactly when a busy period starts
    does not overlap, so back-to-back appointments remain bookable."""
    return slot.start < busy.end and busy.start < slot.end


def subtract_busy(slots: list[TimeSlot], busy_periods: list[TimeSlot]) -> list[TimeSlot]:
    return [s for s in slots if not any(overlaps(s, b) for b in busy_periods)]


def free_bay(slot: TimeSlot, bay_bookings: dict[int, list[TimeSlot]], bays: int) -> int | None:
    """The lowest-numbered bay with nothing overlapping `slot`, or None.

    Lowest-first keeps bookings packed into the same bays, which is how a
    service writer fills a board and leaves whole bays free for long jobs."""
    for bay in range(1, max(1, bays) + 1):
        if not any(overlaps(slot, booked) for booked in bay_bookings.get(bay, [])):
            return bay
    return None


def available_slots(
    *,
    business_hours: dict[str, list[list[str]]],
    timezone: str,
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    busy_periods: list[TimeSlot],
    now: datetime,
    min_notice_hours: int = 0,
    step_minutes: int = DEFAULT_STEP_MINUTES,
    bay_bookings: dict[int, list[TimeSlot]] | None = None,
    bays: int = 1,
) -> list[TimeSlot]:
    """`busy_periods` block the whole business (closures, the calendar's own
    events). `bay_bookings` are the business's appointments by bay: a slot
    stays bookable while at least one of `bays` is free for all of it."""
    candidates = generate_candidate_slots(
        business_hours=business_hours,
        timezone=timezone,
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
        now=now,
        min_notice_hours=min_notice_hours,
        step_minutes=step_minutes,
    )
    open_slots = subtract_busy(candidates, busy_periods)
    if not bay_bookings:
        return open_slots
    return [s for s in open_slots if free_bay(s, bay_bookings, bays) is not None]


def pick_offers(
    slots: list[TimeSlot],
    limit: int,
    *,
    preferred: datetime | None = None,
    min_gap_minutes: int = 90,
) -> list[TimeSlot]:
    """Choose which open slots to say out loud.

    A listener can't scan a list, so each offer has to be a real choice:
    "7:30, 7:45 or 8" is three ways of saying the same thing. Offers are at
    least `min_gap_minutes` apart where possible, starting from the slot
    nearest the caller's preferred time (else the soonest), and are returned
    in time order for speaking.
    """
    if not slots or limit <= 0:
        return []
    if preferred is not None:
        candidates = sorted(slots, key=lambda s: (abs(s.start - preferred), s.start))
    else:
        candidates = sorted(slots, key=lambda s: s.start)
    gap = timedelta(minutes=min_gap_minutes)
    chosen: list[TimeSlot] = []
    for slot in candidates:
        if all(abs(slot.start - c.start) >= gap for c in chosen):
            chosen.append(slot)
        if len(chosen) == limit:
            break
    # Too few spread-out slots (a nearly full day): fill with the nearest.
    for slot in candidates:
        if len(chosen) == limit:
            break
        if slot not in chosen:
            chosen.append(slot)
    return sorted(chosen, key=lambda s: s.start)


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def spoken_day(day: date, today: date, *, with_date: bool = False) -> str:
    """How a person names a day on the phone, relative to today.

    A bare weekday is only unambiguous within the coming week: said on a
    Tuesday, "Tuesday" means today to most callers, so a slot a week out has
    to carry its date. `with_date` (used when confirming a booking) always
    adds it, since the confirmation is what the caller writes down.
    """
    delta = (day - today).days
    dated = f"{day:%B} {_ordinal(day.day)}"
    if delta == 0:
        return f"today, {dated}" if with_date else "today"
    if delta == 1:
        return f"tomorrow, {dated}" if with_date else "tomorrow"
    if 2 <= delta <= 6 and not with_date:
        return f"{day:%A}"
    return f"{day:%A}, {dated}"


def spoken_time(local: datetime) -> str:
    if local.hour == 12 and local.minute == 0:
        return "noon"
    minutes = f":{local.minute:02d}" if local.minute else ""
    meridiem = "am" if local.hour < 12 else "pm"
    return f"{local.hour % 12 or 12}{minutes} {meridiem}"


def _join_or(items: list[str], *, nested: bool = False) -> str:
    """ "a", "a or b", "a, b, or c". `nested` puts a comma before the "or" of
    a pair whose items contain their own "or", so the boundary is heard:
    "today at 3 pm or 4:30 pm, or tomorrow at 9 am"."""
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]}{',' if nested else ''} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"


def describe_slots(
    slots: list[TimeSlot],
    timezone: str,
    limit: int = 3,
    *,
    now: datetime,
    with_date: bool = False,
) -> str:
    """Render slots the way a receptionist would say them out loud: the tool
    result is spoken to the caller, so no ISO timestamps or 24h clocks, each
    day named once ("tomorrow at 9 am, 11 am, or 2 pm"), and days named
    relative to today in the business's timezone (see `spoken_day`)."""
    if not slots:
        return ""
    tz = ZoneInfo(timezone)
    today = now.astimezone(tz).date()
    groups: list[tuple[date, list[str]]] = []
    for slot in slots[:limit]:
        local = slot.start.astimezone(tz)
        if groups and groups[-1][0] == local.date():
            groups[-1][1].append(spoken_time(local))
        else:
            groups.append((local.date(), [spoken_time(local)]))
    phrases = [
        f"{spoken_day(day, today, with_date=with_date)} at {_join_or(times)}"
        for day, times in groups
    ]
    return _join_or(phrases, nested=any(len(times) > 1 for _, times in groups))
