"""Google Calendar adapter contract tests.

Everywhere else the calendar is a fake, which proves our logic but says
nothing about whether we speak Google's wire format correctly. These tests
drive the real adapter against `httpx.MockTransport`, so they lock down the
parts that only fail against the live API: request shape, response parsing,
token refresh, and how each error status is interpreted.

They do not prove Google still behaves this way — that needs the live M3
booking test. They prove we hold up our end of the contract as documented,
and that a change to the adapter can't silently alter it.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.errors import AppError, IntegrationError
from app.core.security import CredentialCipher
from app.modules.scheduling.providers.base import BookingRequest, TimeSlot
from app.modules.scheduling.providers.google import (
    GoogleCalendarProvider,
    _google_event_id,
)
from tests.conftest import requires_services

WINDOW_START = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(days=1)
ENCRYPTION_KEY = Fernet.generate_key().decode()


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "app_env": "test",
        "credentials_encryption_key": ENCRYPTION_KEY,
        "google_oauth_client_id": "client-id",
        "google_oauth_client_secret": "client-secret",
    }
    return Settings(**{**base, **overrides})


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


@pytest.fixture
def connected_tenant():
    """A tenant with a connected Google Calendar integration holding a live
    (non-expired) access token. Yields (tenant_id, integration_id)."""
    yield from _make_tenant(
        {
            "access_token": "access-token-1",
            "refresh_token": "refresh-token-1",
            "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )


@pytest.fixture
def expired_tenant():
    yield from _make_tenant(
        {
            "access_token": "stale-token",
            "refresh_token": "refresh-token-1",
            "expiry": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        }
    )


def _make_tenant(credentials: dict, *, status: str = "connected"):
    tenant_id, integration_id = uuid.uuid4(), uuid.uuid4()
    cipher = CredentialCipher(ENCRYPTION_KEY)
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) "
                "VALUES (:id, :slug, 'Google Test', 'dental')"
            ),
            {"id": str(tenant_id), "slug": f"gcal-{tenant_id.hex[:8]}"},
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO integrations (id, tenant_id, provider, credentials_encrypted, "
                "config, status) VALUES (:id, :tid, 'google_calendar', :creds, "
                "CAST(:config AS jsonb), :status)"
            ),
            {
                "id": str(integration_id),
                "tid": str(tenant_id),
                "creds": cipher.encrypt(json.dumps(credentials)),
                "config": json.dumps({"calendar_id": "practice@example.com"}),
                "status": status,
            },
        )
    try:
        yield tenant_id, integration_id
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


def _mock(provider: GoogleCalendarProvider, handler) -> list[httpx.Request]:
    """Route the adapter's HTTP calls to `handler`, recording requests."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider._http = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(_record), timeout=5
    )
    return seen


# ── freeBusy ───────────────────────────────────────────────────────────────


@requires_services
async def test_free_busy_request_shape_and_parsing(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    seen = _mock(
        provider,
        lambda r: httpx.Response(
            200,
            json={
                "calendars": {
                    "practice@example.com": {
                        "busy": [
                            {"start": "2026-09-15T14:00:00Z", "end": "2026-09-15T15:00:00Z"},
                            {
                                "start": "2026-09-15T18:00:00+00:00",
                                "end": "2026-09-15T18:30:00+00:00",
                            },
                        ]
                    }
                }
            },
        ),
    )

    busy = await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)

    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/freeBusy")
    assert seen[0].headers["Authorization"] == "Bearer access-token-1"
    body = json.loads(seen[0].content)
    assert body["items"] == [{"id": "practice@example.com"}]
    assert body["timeMin"] == WINDOW_START.isoformat()
    assert body["timeMax"] == WINDOW_END.isoformat()

    # Both Google spellings of UTC ("Z" and "+00:00") must parse, and the
    # results must be timezone-aware or every later comparison breaks.
    assert busy == [
        TimeSlot(
            start=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
            end=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
        ),
        TimeSlot(
            start=datetime(2026, 9, 15, 18, 0, tzinfo=UTC),
            end=datetime(2026, 9, 15, 18, 30, tzinfo=UTC),
        ),
    ]


@requires_services
async def test_free_busy_empty_calendar_is_no_busy_periods(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(
        provider,
        lambda r: httpx.Response(200, json={"calendars": {"practice@example.com": {"busy": []}}}),
    )
    assert await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END) == []


@requires_services
async def test_free_busy_per_calendar_error_is_not_silently_empty(connected_tenant) -> None:
    """Google returns HTTP 200 with a per-calendar `errors` array when e.g.
    the calendar id is wrong. Treating that as 'no busy periods' would mark
    the whole day free and double-book the practice — it must raise."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(
        provider,
        lambda r: httpx.Response(
            200,
            json={
                "calendars": {
                    "practice@example.com": {"errors": [{"domain": "global", "reason": "notFound"}]}
                }
            },
        ),
    )
    with pytest.raises(IntegrationError, match="freeBusy error"):
        await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)


@requires_services
async def test_free_busy_http_error_raises(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(500, json={"error": "backend error"}))
    with pytest.raises(IntegrationError, match="freeBusy failed: 500"):
        await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)


# ── booking ────────────────────────────────────────────────────────────────


def _booking(tenant_id: uuid.UUID, **overrides) -> BookingRequest:
    base = {
        "tenant_id": tenant_id,
        "slot": TimeSlot(start=WINDOW_START, end=WINDOW_START + timedelta(hours=1)),
        "summary": "Cleaning — Dana",
        "attendee_name": "Dana",
        "attendee_phone": "+15550001111",
        "attendee_email": "dana@example.com",
        "idempotency_key": "call:abc:2026-09-15T12:00:00+00:00",
    }
    return BookingRequest(**{**base, **overrides})


@requires_services
async def test_event_insert_shape(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "evt-created"}))

    result = await provider.book(_booking(tenant_id))

    assert result.external_event_id == "evt-created"
    assert result.confirmed is True
    assert seen[0].url.path == "/calendar/v3/calendars/practice@example.com/events"
    body = json.loads(seen[0].content)
    assert body["summary"] == "Cleaning — Dana"
    assert body["start"]["dateTime"] == WINDOW_START.isoformat()
    assert body["end"]["dateTime"] == (WINDOW_START + timedelta(hours=1)).isoformat()
    assert body["attendees"] == [{"email": "dana@example.com"}]
    # The caller's phone belongs in the description — the practice needs a way
    # to reach them and Google has no field for it.
    assert "+15550001111" in body["description"]


@requires_services
async def test_booking_without_email_sends_no_attendees(connected_tenant) -> None:
    """Most callers give no email. Sending `[{"email": None}]` would be
    rejected by Google, so the key must be an empty list instead."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "evt-1"}))

    await provider.book(_booking(tenant_id, attendee_email=None))

    assert json.loads(seen[0].content)["attendees"] == []


@requires_services
async def test_idempotency_key_becomes_a_deterministic_event_id(connected_tenant) -> None:
    """Google dedupes on the event id, so the same booking must always
    produce the same id — that is what makes a retry a no-op rather than a
    duplicate event in the practice's calendar."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "evt-1"}))

    await provider.book(_booking(tenant_id))
    await provider.book(_booking(tenant_id))

    ids = [json.loads(r.content)["id"] for r in seen]
    assert ids[0] == ids[1]
    assert ids[0] == _google_event_id("call:abc:2026-09-15T12:00:00+00:00")


@requires_services
async def test_duplicate_event_id_is_treated_as_the_existing_booking(connected_tenant) -> None:
    """409 means the event id already exists — i.e. this exact booking was
    already written. That is success, not failure: raising would make
    `scheduling.service` release a slot that is genuinely booked."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(409, json={"error": "duplicate"}))

    result = await provider.book(_booking(tenant_id))

    assert result.confirmed is True
    assert result.external_event_id == _google_event_id("call:abc:2026-09-15T12:00:00+00:00")


@requires_services
async def test_booking_http_error_raises(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(403, json={"error": "rate limit"}))
    with pytest.raises(IntegrationError, match="event insert failed: 403"):
        await provider.book(_booking(tenant_id))


def test_generated_event_ids_satisfy_googles_charset() -> None:
    """Google accepts only lowercase a-v and 0-9, length 5-1024. An id
    outside that set is rejected at insert time — i.e. no booking."""
    allowed = set("abcdefghijklmnopqrstuv0123456789")
    for key in ("call:abc:2026-09-15T12:00:00+00:00", "anon:x:y", "", "UPPER/CASE+chars"):
        event_id = _google_event_id(key)
        assert 5 <= len(event_id) <= 1024
        assert set(event_id) <= allowed, f"{event_id!r} contains characters Google rejects"


def test_event_ids_differ_between_different_keys() -> None:
    assert _google_event_id("call:a:t") != _google_event_id("call:b:t")


# ── cancellation ───────────────────────────────────────────────────────────


@requires_services
async def test_cancel_sends_delete(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(204))

    await provider.cancel(tenant_id, "evt-1")

    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/calendar/v3/calendars/practice@example.com/events/evt-1"


@requires_services
@pytest.mark.parametrize("status", [404, 410])
async def test_cancelling_an_already_gone_event_is_success(connected_tenant, status) -> None:
    """404/410 mean the event is already absent, which is the desired end
    state. Raising would make a retried cancellation fail forever."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(status, json={"error": "gone"}))
    await provider.cancel(tenant_id, "evt-1")  # must not raise


@requires_services
async def test_cancel_other_errors_raise(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(IntegrationError, match="event delete failed: 500"):
        await provider.cancel(tenant_id, "evt-1")


# ── credentials and token refresh ──────────────────────────────────────────


@requires_services
async def test_expired_token_is_refreshed_before_use(expired_tenant) -> None:
    tenant_id, _ = expired_tenant
    provider = GoogleCalendarProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        return httpx.Response(200, json={"calendars": {"practice@example.com": {"busy": []}}})

    seen = _mock(provider, handler)
    await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)

    assert seen[0].url.host == "oauth2.googleapis.com"
    refresh_body = dict(httpx.QueryParams(seen[0].content.decode()))
    assert refresh_body["grant_type"] == "refresh_token"
    assert refresh_body["refresh_token"] == "refresh-token-1"
    assert refresh_body["client_id"] == "client-id"
    # The freeBusy call must then use the NEW token, not the stale one.
    assert seen[1].headers["Authorization"] == "Bearer fresh-token"


@requires_services
async def test_refreshed_token_is_persisted_for_the_next_call(expired_tenant) -> None:
    """Without persistence every call would refresh, burning quota and
    eventually tripping Google's rate limits."""
    tenant_id, _ = expired_tenant
    provider = GoogleCalendarProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        return httpx.Response(200, json={"calendars": {"practice@example.com": {"busy": []}}})

    seen = _mock(provider, handler)
    await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)
    await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)

    refreshes = [r for r in seen if r.url.host == "oauth2.googleapis.com"]
    assert len(refreshes) == 1, "token was refreshed again despite being freshly stored"
    assert seen[-1].headers["Authorization"] == "Bearer fresh-token"


@requires_services
async def test_revoked_grant_is_marked_so_the_dashboard_can_prompt(expired_tenant) -> None:
    """A revoked refresh token never recovers by retrying. The integration is
    marked so the practice can be asked to reconnect, rather than every call
    failing silently forever."""
    tenant_id, integration_id = expired_tenant
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(IntegrationError, match="token refresh failed"):
        await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)

    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        status = conn.execute(
            text("SELECT status FROM integrations WHERE id = :id"), {"id": str(integration_id)}
        ).scalar_one()
    engine.dispose()
    assert status == "revoked"


@requires_services
async def test_a_tenant_without_the_integration_is_a_clear_error(connected_tenant) -> None:
    """Not a crash, and not silence — `scheduling.tools` turns this into
    'let me take a message'."""
    provider = GoogleCalendarProvider(_settings())
    _mock(provider, lambda r: httpx.Response(200, json={}))
    with pytest.raises(IntegrationError, match="no connected Google Calendar"):
        await provider.list_busy_periods(uuid.uuid4(), WINDOW_START, WINDOW_END)


@requires_services
async def test_disconnected_integration_is_not_used() -> None:
    """A row left behind after a revoked grant must not be treated as usable."""
    generator = _make_tenant({"access_token": "t", "refresh_token": "r"}, status="revoked")
    tenant_id, _ = next(generator)
    try:
        provider = GoogleCalendarProvider(_settings())
        _mock(provider, lambda r: httpx.Response(200, json={}))
        with pytest.raises(IntegrationError, match="no connected Google Calendar"):
            await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)
    finally:
        next(generator, None)


@requires_services
async def test_missing_encryption_key_fails_at_use_not_at_startup(connected_tenant) -> None:
    """The provider is constructed for every deployment during app startup,
    but only ones that actually book need a key. Constructing the cipher
    eagerly would turn a missing optional secret into a failure to boot."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings(credentials_encryption_key=""))  # constructs fine
    _mock(provider, lambda r: httpx.Response(200, json={}))
    with pytest.raises(AppError, match="CREDENTIALS_ENCRYPTION_KEY"):
        await provider.list_busy_periods(tenant_id, WINDOW_START, WINDOW_END)


def test_expiry_is_checked_with_a_skew_not_at_the_exact_second() -> None:
    """A token expiring in 30 seconds is useless on a call that is about to
    make two requests — treat it as expired and refresh early."""
    assert GoogleCalendarProvider._is_expired(
        {"expiry": (datetime.now(UTC) + timedelta(seconds=30)).isoformat()}
    )
    assert not GoogleCalendarProvider._is_expired(
        {"expiry": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}
    )
    # No expiry recorded means we cannot tell; don't refresh on every call.
    assert not GoogleCalendarProvider._is_expired({})


@requires_services
async def test_hot_path_requests_carry_a_timeout(connected_tenant) -> None:
    """Availability runs while the caller waits. An untimed request would
    hold the call in silence until the vendor gave up."""
    provider = GoogleCalendarProvider(_settings())
    client = provider._http()
    try:
        assert client.timeout.connect is not None
        assert client.timeout.read is not None and client.timeout.read <= 10
    finally:
        await client.aclose()


@requires_services
@pytest.mark.parametrize(("blocks_time", "expected"), [(True, "opaque"), (False, "transparent")])
async def test_event_transparency_follows_blocks_time(
    connected_tenant, blocks_time, expected
) -> None:
    """Multi-bay tenants book transparent events, which Google leaves out of
    free/busy; everyone else's bookings keep blocking time as before."""
    tenant_id, _ = connected_tenant
    provider = GoogleCalendarProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(200, json={"id": "evt"}))
    await provider.book(_booking(tenant_id, blocks_time=blocks_time))
    assert json.loads(seen[0].content)["transparency"] == expected
