"""Hot-path deadlines: a caller on the phone must hear a fallback, not silence,
when an upstream hangs — and booking must never be cancelled mid-flight."""

import asyncio
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.modules.conversation.faq import NO_ANSWER_FALLBACK
from app.modules.conversation.providers.anthropic_llm import AnthropicLLMProvider
from app.modules.conversation.providers.voyage import VoyageEmbeddingProvider
from app.modules.conversation.tools import ToolExecutor
from app.modules.scheduling.tools import COULD_NOT_CHECK
from tests.conftest import requires_services
from tests.fakes import FakeCalendarProvider, FakeEmbeddingProvider, FakeLLMProvider
from tests.test_scheduling_tools import FakeRedis, booking_tenant  # noqa: F401
from tests.test_tool_dispatch import _agent_config, _tenant

DEADLINE = 0.1
HANG = 5.0


class HangingEmbeddingProvider(FakeEmbeddingProvider):
    async def embed(self, texts, *, for_query=False):
        await asyncio.sleep(HANG)
        return await super().embed(texts, for_query=for_query)


class SlowCalendarProvider(FakeCalendarProvider):
    def __init__(self, *, busy_delay: float = 0.0, book_delay: float = 0.0) -> None:
        super().__init__()
        self._busy_delay = busy_delay
        self._book_delay = book_delay

    async def list_busy_periods(self, tenant_id, window_start, window_end):
        await asyncio.sleep(self._busy_delay)
        return await super().list_busy_periods(tenant_id, window_start, window_end)

    async def book(self, request):
        await asyncio.sleep(self._book_delay)
        return await super().book(request)


def _future_weekday_10am_local(tz: str = "America/New_York") -> str:
    day = datetime.now(ZoneInfo(tz)) + timedelta(days=14)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.strftime("%Y-%m-%dT10:00")


async def test_hanging_faq_dependency_returns_the_spoken_fallback_promptly() -> None:
    executor = ToolExecutor(
        FakeLLMProvider(), HangingEmbeddingProvider(), read_deadline_seconds=DEADLINE
    )
    started = time.monotonic()
    result = await executor.dispatch(
        tenant=_tenant(),
        call_id=uuid.uuid4(),
        caller_e164="+15550001111",
        agent_config=_agent_config(),
        tool_name="answer_faq",
        arguments={"question": "What are your hours?"},
    )
    assert result == NO_ANSWER_FALLBACK
    assert time.monotonic() - started < HANG / 2


@requires_services
async def test_hanging_calendar_read_returns_could_not_check(booking_tenant) -> None:  # noqa: F811
    executor = ToolExecutor(
        FakeLLMProvider(),
        FakeEmbeddingProvider(),
        calendar=SlowCalendarProvider(busy_delay=HANG),
        read_deadline_seconds=DEADLINE,
    )
    started = time.monotonic()
    result = await executor.dispatch(
        tenant=booking_tenant,
        call_id=uuid.uuid4(),
        caller_e164="+15550001111",
        agent_config=_agent_config(),
        tool_name="check_availability",
        arguments={"service": "cleaning", "preferred_time": _future_weekday_10am_local()},
    )
    assert result == COULD_NOT_CHECK
    assert time.monotonic() - started < HANG / 2


@requires_services
async def test_booking_is_not_cancelled_by_the_read_deadline(booking_tenant) -> None:  # noqa: F811
    """The calendar write takes longer than the read deadline. Cancelling it
    would skip the release-on-failure path and could strand a reserved slot,
    so the booking must run to completion and report success."""
    calendar = SlowCalendarProvider(book_delay=0.4)
    executor = ToolExecutor(
        FakeLLMProvider(),
        FakeEmbeddingProvider(),
        calendar=calendar,
        redis=FakeRedis(),
        read_deadline_seconds=0.05,
    )
    result = await executor.dispatch(
        tenant=booking_tenant,
        call_id=None,
        caller_e164="+15550001111",
        agent_config=_agent_config(),
        tool_name="book_appointment",
        arguments={
            "service": "cleaning",
            "starts_at": _future_weekday_10am_local(),
            "customer_name": "Dana",
        },
    )
    assert "all set" in result.lower(), result
    assert len(calendar.booked) == 1

    engine = create_engine(
        Settings(_env_file=None, app_env="test").database_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg2://", 1
        )
    )
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(booking_tenant.id)}
        )
        statuses = [
            r[0]
            for r in conn.execute(
                text("SELECT status FROM appointments WHERE tenant_id = :t"),
                {"t": str(booking_tenant.id)},
            )
        ]
    engine.dispose()
    assert statuses == ["confirmed"]


def test_sdk_clients_are_bounded_by_settings_not_sdk_defaults() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        llm_timeout_seconds=12.0,
        llm_max_retries=1,
        embedding_timeout_seconds=4.0,
        embedding_max_retries=0,
    )
    llm_client = AnthropicLLMProvider(settings)._client
    assert llm_client.timeout.read == 12.0
    assert llm_client.timeout.connect == 5.0
    assert llm_client.max_retries == 1

    embed_client = VoyageEmbeddingProvider(settings)._client
    assert embed_client._params["request_timeout"] == 4.0
    assert embed_client.max_retries == 0


def test_default_bounds_are_far_below_the_sdk_defaults() -> None:
    settings = Settings(_env_file=None, app_env="test")
    assert settings.llm_timeout_seconds <= 60  # the SDK default read timeout is 600s
    assert settings.tool_deadline_seconds <= 10
