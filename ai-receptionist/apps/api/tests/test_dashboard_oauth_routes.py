"""Integration connect flow: authorize-URL construction, state signing, code
exchange (against httpx.MockTransport, never the real network), and the
callback route end-to-end.
"""

import time
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.errors import (
    AuthenticationError,
    IntegrationError,
    PermanentIntegrationError,
    ValidationFailedError,
)
from app.core.security import decode_jwt, hash_password
from app.main import create_app
from app.modules.tenants import oauth
from tests.conftest import requires_services

CONNECTED_SETTINGS = dict(
    _env_file=None,
    app_env="test",
    google_oauth_client_id="g-id",
    google_oauth_client_secret="g-secret",
    hubspot_client_id="h-id",
    hubspot_client_secret="h-secret",
    public_webhook_base_url="https://api.example.com",
    dashboard_base_url="https://dashboard.example.com",
    credentials_encryption_key=Fernet.generate_key().decode(),
)


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


def _seed_tenant_and_owner(engine) -> tuple[uuid.UUID, str]:
    tenant_id = uuid.uuid4()
    slug = f"oauth-{uuid.uuid4().hex[:8]}"
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
    return tenant_id, slug


# ── build_authorize_url / decode_state (no DB, no network) ─────────────


def test_google_authorize_url_has_offline_access_and_consent_prompt() -> None:
    """Without these two params Google only returns a refresh token on the
    very first consent grant — a reconnect after a revoke silently leaves
    the tenant unable to refresh."""
    settings = Settings(**CONNECTED_SETTINGS)
    url = oauth.build_authorize_url(
        provider="google_calendar", tenant_id=uuid.uuid4(), settings=settings
    )
    params = parse_qs(urlparse(url).query)
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["redirect_uri"] == [
        "https://api.example.com/api/v1/integrations/google_calendar/callback"
    ]


def test_hubspot_authorize_url_shape() -> None:
    settings = Settings(**CONNECTED_SETTINGS)
    url = oauth.build_authorize_url(provider="hubspot", tenant_id=uuid.uuid4(), settings=settings)
    assert url.startswith(oauth.HUBSPOT_AUTHORIZE_URL)
    params = parse_qs(urlparse(url).query)
    assert params["client_id"] == ["h-id"]


def test_unknown_provider_is_rejected() -> None:
    settings = Settings(**CONNECTED_SETTINGS)
    with pytest.raises(ValidationFailedError):
        oauth.build_authorize_url(provider="salesforce", tenant_id=uuid.uuid4(), settings=settings)


def test_missing_client_id_fails_loudly() -> None:
    settings = Settings(**{**CONNECTED_SETTINGS, "google_oauth_client_id": ""})
    with pytest.raises(IntegrationError, match="GOOGLE_OAUTH_CLIENT_ID"):
        oauth.build_authorize_url(
            provider="google_calendar", tenant_id=uuid.uuid4(), settings=settings
        )


def test_state_round_trips_tenant_and_provider() -> None:
    settings = Settings(**CONNECTED_SETTINGS)
    tenant_id = uuid.uuid4()
    url = oauth.build_authorize_url(provider="hubspot", tenant_id=tenant_id, settings=settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    decoded_tenant, provider = oauth.decode_state(state, settings=settings)
    assert decoded_tenant == tenant_id
    assert provider == "hubspot"


def test_state_cannot_be_replayed_as_a_login_token() -> None:
    """A leaked OAuth state must not work as a session token even though
    it's signed with the same secret — `typ` is what prevents that."""
    settings = Settings(**CONNECTED_SETTINGS)
    url = oauth.build_authorize_url(provider="hubspot", tenant_id=uuid.uuid4(), settings=settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    payload = decode_jwt(state, secret_key=settings.secret_key)
    assert payload["typ"] == "oauth_state"


def test_expired_state_is_rejected() -> None:
    settings = Settings(**{**CONNECTED_SETTINGS, "oauth_state_ttl_minutes": 0})
    url = oauth.build_authorize_url(provider="hubspot", tenant_id=uuid.uuid4(), settings=settings)
    state = parse_qs(urlparse(url).query)["state"][0]
    time.sleep(1.1)
    with pytest.raises(AuthenticationError):
        oauth.decode_state(state, settings=settings)


# ── exchange_code (mocked transport, no real network) ────────────────


async def test_exchange_code_google_happy_path() -> None:
    settings = Settings(**CONNECTED_SETTINGS)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == oauth.GOOGLE_TOKEN_URL
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["code"] == "authcode"
        assert body["grant_type"] == "authorization_code"
        return httpx.Response(
            200, json={"access_token": "gat", "refresh_token": "grt", "expires_in": 3600}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    creds = await oauth.exchange_code(
        provider="google_calendar", code="authcode", settings=settings, client=client
    )
    await client.aclose()
    assert creds["access_token"] == "gat"
    assert creds["refresh_token"] == "grt"
    assert "expiry" in creds


async def test_exchange_code_hubspot_happy_path() -> None:
    settings = Settings(**CONNECTED_SETTINGS)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == oauth.HUBSPOT_TOKEN_URL
        return httpx.Response(
            200, json={"access_token": "hat", "refresh_token": "hrt", "expires_in": 1800}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    creds = await oauth.exchange_code(
        provider="hubspot", code="authcode", settings=settings, client=client
    )
    await client.aclose()
    assert creds["access_token"] == "hat"


async def test_exchange_code_http_error_is_permanent() -> None:
    """A code exchange is one-shot and user-interactive — retrying can't
    help, so this must not be classified as the queue's transient
    IntegrationError."""
    settings = Settings(**CONNECTED_SETTINGS)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    with pytest.raises(PermanentIntegrationError):
        await oauth.exchange_code(
            provider="google_calendar", code="bad", settings=settings, client=client
        )
    await client.aclose()


async def test_exchange_code_missing_refresh_token_is_permanent() -> None:
    """A credential set with no refresh token silently stops working the
    moment the access token expires — better to fail the connect flow now."""
    settings = Settings(**CONNECTED_SETTINGS)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"access_token": "gat", "expires_in": 3600})
        )
    )
    with pytest.raises(PermanentIntegrationError, match="refresh token"):
        await oauth.exchange_code(
            provider="google_calendar", code="authcode", settings=settings, client=client
        )
    await client.aclose()


# ── routes: list / connect ──────────────────────────────────────────────


@requires_services
def test_list_integrations_shows_not_connected_by_default() -> None:
    engine = _sync_engine()
    tenant_id, slug = _seed_tenant_and_owner(engine)
    try:
        with TestClient(create_app(Settings(**CONNECTED_SETTINGS))) as client:
            token = client.post(
                "/api/v1/auth/login",
                json={"tenant_slug": slug, "email": "owner@x.com", "password": "pw"},
            ).json()["access_token"]
            r = client.get("/api/v1/integrations", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
            statuses = {row["provider"]: row["connected"] for row in r.json()}
            assert statuses == {"google_calendar": False, "hubspot": False}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_connect_returns_an_authorize_url_containing_signed_state() -> None:
    engine = _sync_engine()
    tenant_id, slug = _seed_tenant_and_owner(engine)
    try:
        with TestClient(create_app(Settings(**CONNECTED_SETTINGS))) as client:
            token = client.post(
                "/api/v1/auth/login",
                json={"tenant_slug": slug, "email": "owner@x.com", "password": "pw"},
            ).json()["access_token"]
            r = client.get(
                "/api/v1/integrations/google_calendar/connect",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200
            assert r.json()["authorize_url"].startswith(oauth.GOOGLE_AUTHORIZE_URL)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


# ── routes: callback (exchange_code mocked at the route level) ─────────


@requires_services
def test_callback_happy_path_stores_credentials_and_redirects_to_dashboard() -> None:
    engine = _sync_engine()
    tenant_id, slug = _seed_tenant_and_owner(engine)
    try:
        settings = Settings(**CONNECTED_SETTINGS)
        state = oauth.build_authorize_url(
            provider="google_calendar", tenant_id=tenant_id, settings=settings
        )
        state_param = parse_qs(urlparse(state).query)["state"][0]

        fake_creds = {
            "access_token": "gat",
            "refresh_token": "grt",
            "expiry": "2030-01-01T00:00:00+00:00",
        }
        with (
            patch(
                "app.modules.tenants.oauth_routes.exchange_code",
                new=AsyncMock(return_value=fake_creds),
            ),
            TestClient(create_app(settings)) as client,
        ):
            r = client.get(
                f"/api/v1/integrations/google_calendar/callback?code=authcode&state={state_param}",
                follow_redirects=False,
            )
            assert r.status_code in (302, 307)
            assert (
                r.headers["location"]
                == "https://dashboard.example.com/settings?connected=google_calendar"
            )

        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
            )
            status = conn.execute(
                text(
                    "SELECT status FROM integrations "
                    "WHERE tenant_id = :tid AND provider = 'google_calendar'"
                ),
                {"tid": str(tenant_id)},
            ).scalar_one()
        assert status == "connected"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
def test_callback_denied_consent_redirects_with_error_not_a_json_500() -> None:
    with TestClient(create_app(Settings(**CONNECTED_SETTINGS))) as client:
        r = client.get(
            "/api/v1/integrations/google_calendar/callback?error=access_denied",
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
        assert "error=oauth_denied" in r.headers["location"]


@requires_services
def test_callback_missing_code_redirects_with_error() -> None:
    with TestClient(create_app(Settings(**CONNECTED_SETTINGS))) as client:
        r = client.get(
            "/api/v1/integrations/google_calendar/callback?state=whatever",
            follow_redirects=False,
        )
        assert "error=oauth_callback_invalid" in r.headers["location"]


@requires_services
def test_callback_provider_mismatch_is_rejected() -> None:
    """A state signed for hubspot presented at the google_calendar callback
    URL — tampering, or a stale bookmark — must not connect anything."""
    settings = Settings(**CONNECTED_SETTINGS)
    url = oauth.build_authorize_url(provider="hubspot", tenant_id=uuid.uuid4(), settings=settings)
    state_param = parse_qs(urlparse(url).query)["state"][0]
    with TestClient(create_app(settings)) as client:
        r = client.get(
            f"/api/v1/integrations/google_calendar/callback?code=x&state={state_param}",
            follow_redirects=False,
        )
        assert "error=oauth_callback_invalid" in r.headers["location"]


@requires_services
def test_callback_with_garbage_state_is_rejected() -> None:
    with TestClient(create_app(Settings(**CONNECTED_SETTINGS))) as client:
        r = client.get(
            "/api/v1/integrations/google_calendar/callback?code=x&state=garbage",
            follow_redirects=False,
        )
        assert "error=" in r.headers["location"]
