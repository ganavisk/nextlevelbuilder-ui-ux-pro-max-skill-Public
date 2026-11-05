"""Calls, appointments, callback-requests, analytics, and CRM failed-syncs
dashboard routes — driven through the real HTTP routes against live
Postgres, with a seeded tenant carrying one of everything.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.main import create_app
from tests.conftest import requires_services
from tests.fakes import FakeCalendarProvider


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


NOW = datetime.now(UTC)


@pytest.fixture
def seeded():
    """A tenant with an owner user, one classified call (+transcript), one
    confirmed appointment, and one open callback request — all linked to
    the same call, the way a real booking call produces them. Yields
    (tenant_id, slug, call_id, appointment_id, callback_id)."""
    engine = _sync_engine()
    tenant_id = uuid.uuid4()
    slug = f"data-{uuid.uuid4().hex[:8]}"
    call_id = uuid.uuid4()
    appointment_id = uuid.uuid4()
    callback_id = uuid.uuid4()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) VALUES (:id, :slug, 'T', 'dental')"
            ),
            {"id": str(tenant_id), "slug": slug},
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO users (tenant_id, email, role, password_hash) "
                "VALUES (:tid, 'owner@x.com', 'owner', :hash)"
            ),
            {"tid": str(tenant_id), "hash": hash_password("pw")},
        )
        conn.execute(
            text(
                "INSERT INTO users (tenant_id, email, role, password_hash) "
                "VALUES (:tid, 'viewer@x.com', 'viewer', :hash)"
            ),
            {"tid": str(tenant_id), "hash": hash_password("pw")},
        )
        conn.execute(
            text(
                "INSERT INTO calls (id, tenant_id, vendor_call_id, caller_e164, started_at, "
                "ended_at, outcome, sentiment, summary, recording_url, vendor_cost_cents, "
                "llm_cost_cents) VALUES (:id, :tid, 'vc-1', '+15551234567', :started, :ended, "
                "'appointment_booked', 'positive', 'Booked a cleaning.', 'https://x/r.wav', 12, 3)"
            ),
            {
                "id": str(call_id),
                "tid": str(tenant_id),
                "started": NOW - timedelta(minutes=5),
                "ended": NOW,
            },
        )
        conn.execute(
            text(
                "INSERT INTO transcripts (tenant_id, call_id, turns) VALUES "
                "(:tid, :cid, CAST(:turns AS jsonb))"
            ),
            {
                "tid": str(tenant_id),
                "cid": str(call_id),
                "turns": '[{"role": "assistant", "text": "Hello!"}, '
                '{"role": "caller", "text": "I need a cleaning."}]',
            },
        )
        conn.execute(
            text(
                "INSERT INTO appointments (id, tenant_id, call_id, service, starts_at, ends_at, "
                "status, customer_name, customer_phone, external_event_id) VALUES "
                "(:id, :tid, :cid, 'Cleaning', :starts, :ends, 'confirmed', 'Dana', "
                "'+15551234567', 'evt-abc')"
            ),
            {
                "id": str(appointment_id),
                "tid": str(tenant_id),
                "cid": str(call_id),
                "starts": NOW + timedelta(days=1),
                "ends": NOW + timedelta(days=1, hours=1),
            },
        )
        conn.execute(
            text(
                "INSERT INTO callback_requests (id, tenant_id, call_id, caller_e164, reason, "
                "status) VALUES (:id, :tid, :cid, '+15559999999', 'Wants a human', 'open')"
            ),
            {"id": str(callback_id), "tid": str(tenant_id), "cid": str(call_id)},
        )
    try:
        yield tenant_id, slug, call_id, appointment_id, callback_id
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


def _login_headers(client: TestClient, slug: str, email: str = "owner@x.com") -> dict:
    token = client.post(
        "/api/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": "pw"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ── calls ────────────────────────────────────────────────────────────────


@requires_services
def test_list_calls_returns_the_seeded_call(seeded) -> None:
    tenant_id, slug, call_id, _, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get("/api/v1/calls", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == str(call_id)
        assert body["items"][0]["outcome"] == "appointment_booked"
        # List rows must not carry the transcript — that's the detail
        # endpoint's job (see CallSummary's docstring).
        assert "transcript" not in body["items"][0]


@requires_services
def test_list_calls_filters_by_outcome(seeded) -> None:
    _, slug, _, _, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        matching = client.get("/api/v1/calls?outcome=appointment_booked", headers=headers).json()
        nonmatching = client.get("/api/v1/calls?outcome=abandoned", headers=headers).json()
        assert matching["total"] == 1
        assert nonmatching["total"] == 0


@requires_services
def test_get_call_detail_includes_transcript(seeded) -> None:
    _, slug, call_id, _, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get(f"/api/v1/calls/{call_id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["transcript"] == [
            {"role": "assistant", "text": "Hello!"},
            {"role": "caller", "text": "I need a cleaning."},
        ]
        assert body["vendor_cost_cents"] == 12
        assert body["llm_cost_cents"] == 3


@requires_services
def test_get_call_detail_404_for_unknown_id(seeded) -> None:
    _, slug, _, _, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get(f"/api/v1/calls/{uuid.uuid4()}", headers=headers)
        assert r.status_code == 404


@requires_services
def test_a_tenant_cannot_see_another_tenants_calls(seeded) -> None:
    """RLS enforcement through the dashboard route, not just the webhook
    path — a second tenant, logged in as itself, must see zero calls."""
    _, _, call_id, _, _ = seeded
    engine = _sync_engine()
    other_tenant_id = uuid.uuid4()
    other_slug = f"other-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) VALUES (:id, :slug, 'O', 'dental')"
            ),
            {"id": str(other_tenant_id), "slug": other_slug},
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(other_tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO users (tenant_id, email, role, password_hash) "
                "VALUES (:tid, 'owner@x.com', 'owner', :hash)"
            ),
            {"tid": str(other_tenant_id), "hash": hash_password("pw")},
        )
    try:
        with TestClient(create_app(Settings(app_env="test"))) as client:
            headers = _login_headers(client, other_slug)
            listing = client.get("/api/v1/calls", headers=headers)
            detail = client.get(f"/api/v1/calls/{call_id}", headers=headers)
            assert listing.json()["total"] == 0
            assert detail.status_code == 404
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(other_tenant_id)})
        engine.dispose()


# ── appointments ─────────────────────────────────────────────────────────


@requires_services
def test_list_appointments_returns_the_seeded_appointment(seeded) -> None:
    _, slug, _, appointment_id, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get("/api/v1/appointments", headers=headers)
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["id"] == str(appointment_id)
        assert r.json()["items"][0]["customer_name"] == "Dana"


@requires_services
def test_cancel_appointment_calls_the_calendar_and_flips_status(seeded) -> None:
    _, slug, _, appointment_id, _ = seeded
    calendar = FakeCalendarProvider()
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        app.state.calendar_provider = calendar  # swap in post-startup, see main.py
        headers = _login_headers(client, slug)
        r = client.post(f"/api/v1/appointments/{appointment_id}/cancel", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        assert calendar.cancelled == ["evt-abc"]


@requires_services
def test_cancelling_twice_is_idempotent_not_an_error(seeded) -> None:
    _, slug, _, appointment_id, _ = seeded
    calendar = FakeCalendarProvider()
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        app.state.calendar_provider = calendar
        headers = _login_headers(client, slug)
        first = client.post(f"/api/v1/appointments/{appointment_id}/cancel", headers=headers)
        second = client.post(f"/api/v1/appointments/{appointment_id}/cancel", headers=headers)
        assert first.status_code == second.status_code == 200
        assert second.json()["status"] == "cancelled"
        # The calendar must only ever be told once — a second dashboard
        # click (double-click, retry) must not re-issue the vendor call.
        assert calendar.cancelled == ["evt-abc"]


@requires_services
def test_cancel_appointment_404_for_unknown_id(seeded) -> None:
    _, slug, _, _, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.post(f"/api/v1/appointments/{uuid.uuid4()}/cancel", headers=headers)
        assert r.status_code == 404


# ── callback requests ────────────────────────────────────────────────────


@requires_services
def test_list_callback_requests(seeded) -> None:
    _, slug, _, _, callback_id = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get("/api/v1/callback-requests", headers=headers)
        assert r.status_code == 200
        assert r.json()["items"][0]["id"] == str(callback_id)
        assert r.json()["items"][0]["status"] == "open"


@requires_services
def test_update_callback_status(seeded) -> None:
    _, slug, _, _, callback_id = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.patch(
            f"/api/v1/callback-requests/{callback_id}",
            headers=headers,
            json={"status": "contacted"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "contacted"


@requires_services
def test_update_callback_status_rejects_invalid_status(seeded) -> None:
    _, slug, _, _, callback_id = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.patch(
            f"/api/v1/callback-requests/{callback_id}", headers=headers, json={"status": "bogus"}
        )
        assert r.status_code == 422


# ── analytics ────────────────────────────────────────────────────────────


@requires_services
def test_analytics_summary_reflects_the_seeded_call(seeded) -> None:
    _, slug, _, _, _ = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get("/api/v1/analytics/summary?days=30", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total_calls"] == 1
        assert body["classified_calls"] == 1
        assert body["calls_by_outcome"] == {"appointment_booked": 1}
        assert body["calls_by_sentiment"] == {"positive": 1}
        assert body["booking_rate"] == 1.0
        assert body["escalation_rate"] == 0.0
        assert sum(d["count"] for d in body["daily_call_volume"]) == 1


@requires_services
def test_analytics_summary_window_excludes_older_calls(seeded) -> None:
    """A 0-day window (today only) must not silently include everything —
    proves the window filter is actually applied, not decorative."""
    tenant_id, slug, _, _, _ = seeded
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO calls (tenant_id, vendor_call_id, caller_e164, started_at, outcome) "
                "VALUES (:tid, 'vc-old', '+15550000000', :started, 'abandoned')"
            ),
            {"tid": str(tenant_id), "started": NOW - timedelta(days=60)},
        )
    engine.dispose()
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        recent = client.get("/api/v1/analytics/summary?days=7", headers=headers).json()
        everything = client.get("/api/v1/analytics/summary?days=365", headers=headers).json()
        assert recent["total_calls"] == 1  # only the seeded fixture call
        assert everything["total_calls"] == 2  # fixture call + the 60-day-old one


# ── crm failed-syncs ─────────────────────────────────────────────────────


@requires_services
def test_failed_syncs_lists_only_failed_and_dead(seeded) -> None:
    tenant_id, slug, call_id, _, _ = seeded
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO crm_syncs (tenant_id, call_id, provider, status, attempts, "
                "last_error, idempotency_key) VALUES "
                "(:tid, :cid, 'hubspot', 'failed', 2, 'HubSpot 503', :key)"
            ),
            {"tid": str(tenant_id), "cid": str(call_id), "key": f"call:{call_id}"},
        )
    engine.dispose()
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug)
        r = client.get("/api/v1/crm/failed-syncs", headers=headers)
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["status"] == "failed"
        assert r.json()[0]["last_error"] == "HubSpot 503"


# ── role enforcement ─────────────────────────────────────────────────────


@requires_services
def test_viewer_can_read_but_not_cancel_an_appointment(seeded) -> None:
    """The dashboard hides the Cancel button from viewers, but hiding is not
    enforcement — the API has to refuse, and must leave the calendar alone."""
    _, slug, _, appointment_id, _ = seeded
    calendar = FakeCalendarProvider()
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        app.state.calendar_provider = calendar
        headers = _login_headers(client, slug, "viewer@x.com")
        assert client.get("/api/v1/appointments", headers=headers).status_code == 200
        r = client.post(f"/api/v1/appointments/{appointment_id}/cancel", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"
        assert calendar.cancelled == []


@requires_services
def test_viewer_cannot_change_a_callback_status(seeded) -> None:
    _, slug, _, _, callback_id = seeded
    with TestClient(create_app(Settings(app_env="test"))) as client:
        headers = _login_headers(client, slug, "viewer@x.com")
        r = client.patch(
            f"/api/v1/callback-requests/{callback_id}", headers=headers, json={"status": "resolved"}
        )
        assert r.status_code == 403
        listed = client.get("/api/v1/callback-requests", headers=headers).json()
        assert listed["items"][0]["status"] == "open"
