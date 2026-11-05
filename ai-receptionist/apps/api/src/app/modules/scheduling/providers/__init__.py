"""Calendar provider factory — the single place adapter selection happens."""

from app.core.config import Settings
from app.modules.scheduling.providers.base import CalendarProvider
from app.modules.scheduling.providers.google import GoogleCalendarProvider


def get_calendar_provider(settings: Settings) -> CalendarProvider:
    # Cal.com is the planned second adapter (Phase 1 §5.1); it would abstract
    # several calendar backends behind this same interface.
    return GoogleCalendarProvider(settings)
