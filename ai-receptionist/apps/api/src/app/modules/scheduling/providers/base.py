"""CalendarProvider — booking backends (Google Calendar first; Cal.com later).

The scheduling module owns availability rules, conflict prevention, and
idempotency; this interface only reads free/busy and writes events. The
calendar is the source of truth for bookings — the local `appointments` row
mirrors it for reporting.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TimeSlot:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class BookingRequest:
    tenant_id: uuid.UUID
    slot: TimeSlot
    summary: str
    attendee_name: str
    attendee_phone: str
    attendee_email: str | None = None
    idempotency_key: str | None = None
    # False for multi-bay tenants: the event is informational, and must not show
    # as busy, or one booked bay would make the calendar block the whole shop.
    blocks_time: bool = True


@dataclass(frozen=True)
class BookingResult:
    external_event_id: str
    slot: TimeSlot
    confirmed: bool


class CalendarProvider(ABC):
    @abstractmethod
    async def list_busy_periods(
        self, tenant_id: uuid.UUID, window_start: datetime, window_end: datetime
    ) -> list[TimeSlot]:
        """Periods already occupied on the tenant's calendar.

        Only raw free/busy — business hours, service durations, and notice
        periods are platform policy, not vendor data, so they live in
        `scheduling.availability` where they're testable without a calendar
        and identical across vendors.
        """

    @abstractmethod
    async def book(self, request: BookingRequest) -> BookingResult:
        """Create the event, idempotently on `idempotency_key` — a retry must
        return the existing booking rather than create a second one.

        This is *not* the double-booking guard: no calendar API offers an
        atomic "create if free". Serialization happens in our database (see
        `scheduling.service`), and the calendar write follows it.
        """

    @abstractmethod
    async def cancel(self, tenant_id: uuid.UUID, external_event_id: str) -> None: ...
