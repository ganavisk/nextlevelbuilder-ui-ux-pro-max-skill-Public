"""The real API with an in-memory calendar, for load testing the call path.

Everything is production code (webhook verification, Redis call state,
Postgres with row-level security, the booking constraint, the tool executor)
except the Google Calendar client. That way a load test measures this
platform's own cost and not Google's latency, which is external and capped per
tool call by TOOL_DEADLINE_SECONDS anyway.

    VAPI_WEBHOOK_SECRET=loadtest uvicorn scripts.loadtest_server:app --workers 4

Never deploy this: it accepts bookings without writing to any calendar.
"""

import uuid
from datetime import datetime
from typing import Any

from starlette.requests import Request

from app.main import create_app
from app.modules.scheduling.providers.base import (
    BookingRequest,
    BookingResult,
    CalendarProvider,
    TimeSlot,
)


class InMemoryCalendar(CalendarProvider):
    """No busy periods of its own and instant bookings, so availability and
    capacity come entirely from our database, as they do in production."""

    async def list_busy_periods(
        self, tenant_id: uuid.UUID, window_start: datetime, window_end: datetime
    ) -> list[TimeSlot]:
        return []

    async def book(self, request: BookingRequest) -> BookingResult:
        return BookingResult(
            external_event_id=f"lt-{uuid.uuid4()}", slot=request.slot, confirmed=True
        )

    async def cancel(self, tenant_id: uuid.UUID, external_event_id: str) -> None:
        return None


CALENDAR = InMemoryCalendar()
app = create_app()


@app.middleware("http")
async def _use_in_memory_calendar(request: Request, call_next: Any) -> Any:
    request.app.state.calendar_provider = CALENDAR
    return await call_next(request)
