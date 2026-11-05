"""HubSpot adapter contract tests.

Everywhere else the CRM is a fake, which proves our sync logic but says
nothing about whether we speak HubSpot's wire format correctly. These drive
the real adapter against `httpx.MockTransport` and lock down the parts that
only fail against the live API — especially contact identity, since getting
that wrong duplicates a contact on every call.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.errors import IntegrationError, PermanentIntegrationError
from app.core.security import CredentialCipher
from app.modules.crm.providers.base import ActivityRecord, ContactRecord, DealRecord
from app.modules.crm.providers.hubspot import HubSpotCRMProvider
from tests.conftest import requires_services

ENCRYPTION_KEY = Fernet.generate_key().decode()


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "app_env": "test",
        "credentials_encryption_key": ENCRYPTION_KEY,
        "hubspot_client_id": "client-id",
        "hubspot_client_secret": "client-secret",
    }
    return Settings(**{**base, **overrides})


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


@pytest.fixture
def connected_tenant():
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
                "VALUES (:id, :slug, 'HubSpot Test', 'dental')"
            ),
            {"id": str(tenant_id), "slug": f"hs-{tenant_id.hex[:8]}"},
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO integrations (id, tenant_id, provider, credentials_encrypted, "
                "config, status) VALUES (:id, :tid, 'hubspot', :creds, '{}', :status)"
            ),
            {
                "id": str(integration_id),
                "tid": str(tenant_id),
                "creds": cipher.encrypt(json.dumps(credentials)),
                "status": status,
            },
        )
    try:
        yield tenant_id, integration_id
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


def _mock(provider: HubSpotCRMProvider, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider._http = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(_record), timeout=20
    )
    return seen


# ── contacts ─────────────────────────────────────────────────────────────


@requires_services
async def test_new_contact_with_email_is_created(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(201, json={"id": "contact-1"}))

    contact_id = await provider.upsert_contact(
        tenant_id,
        ContactRecord(phone="+15550001111", name="Dana Lee", email="dana@example.com"),
    )

    assert contact_id == "contact-1"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/crm/v3/objects/contacts"
    body = json.loads(seen[0].content)["properties"]
    assert body["email"] == "dana@example.com"
    assert body["firstname"] == "Dana"
    assert body["lastname"] == "Lee"
    assert body["phone"] == "+15550001111"


@requires_services
async def test_duplicate_email_looks_up_and_updates_the_existing_contact(
    connected_tenant,
) -> None:
    """409 on create means the email already exists. Parsing HubSpot's error
    prose for the id is brittle, so the adapter looks it up via search."""
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts" and request.method == "POST":
            return httpx.Response(409, json={"message": "contact already exists"})
        if request.url.path == "/crm/v3/objects/contacts/search":
            return httpx.Response(200, json={"results": [{"id": "contact-existing"}]})
        if request.url.path == "/crm/v3/objects/contacts/contact-existing":
            return httpx.Response(200, json={"id": "contact-existing"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    seen = _mock(provider, handler)
    contact_id = await provider.upsert_contact(
        tenant_id, ContactRecord(phone="+15550001111", email="dana@example.com")
    )

    assert contact_id == "contact-existing"
    methods_paths = [(r.method, r.url.path) for r in seen]
    assert ("PATCH", "/crm/v3/objects/contacts/contact-existing") in methods_paths


@requires_services
async def test_no_email_matches_by_phone_before_creating(connected_tenant) -> None:
    """Most callers give no email, and HubSpot only dedupes on email. Without
    an explicit phone match, every call from a returning caller creates a
    second contact."""
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts/search":
            body = json.loads(request.content)
            assert body["filterGroups"][0]["filters"][0]["propertyName"] == "phone"
            return httpx.Response(200, json={"results": [{"id": "contact-by-phone"}]})
        if request.url.path == "/crm/v3/objects/contacts/contact-by-phone":
            return httpx.Response(200, json={"id": "contact-by-phone"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    seen = _mock(provider, handler)
    contact_id = await provider.upsert_contact(
        tenant_id, ContactRecord(phone="+15550001111", email=None)
    )

    assert contact_id == "contact-by-phone"
    # Must never fall through to a plain POST create once matched.
    assert not any(r.method == "POST" and r.url.path == "/crm/v3/objects/contacts" for r in seen)


@requires_services
async def test_no_email_and_no_phone_match_creates_a_new_contact(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/contacts/search":
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/crm/v3/objects/contacts" and request.method == "POST":
            return httpx.Response(201, json={"id": "contact-new"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    _mock(provider, handler)
    contact_id = await provider.upsert_contact(
        tenant_id, ContactRecord(phone="+15550009999", email=None)
    )
    assert contact_id == "contact-new"


@requires_services
async def test_single_word_name_has_no_lastname_key(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(201, json={"id": "contact-1"}))

    await provider.upsert_contact(
        tenant_id, ContactRecord(phone="+15550001111", name="Cher", email="cher@example.com")
    )
    body = json.loads(seen[0].content)["properties"]
    assert body["firstname"] == "Cher"
    assert "lastname" not in body


@requires_services
async def test_custom_properties_pass_through(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(201, json={"id": "contact-1"}))

    await provider.upsert_contact(
        tenant_id,
        ContactRecord(
            phone="+15550001111", email="dana@example.com", properties={"urgency": "true"}
        ),
    )
    assert json.loads(seen[0].content)["properties"]["urgency"] == "true"


@requires_services
async def test_contact_permanent_error_does_not_retry_worthy(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    _mock(provider, lambda r: httpx.Response(400, json={"message": "bad property"}))
    with pytest.raises(PermanentIntegrationError, match="contact create rejected"):
        await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email="x@example.com"))


@requires_services
async def test_contact_server_error_is_transient(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    _mock(provider, lambda r: httpx.Response(503, json={"message": "backend"}))
    with pytest.raises(IntegrationError) as exc_info:
        await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email="x@example.com"))
    assert not isinstance(exc_info.value, PermanentIntegrationError)


@requires_services
async def test_rate_limit_is_transient(connected_tenant) -> None:
    """429 must be retried, not dead-lettered — a burst of calls will trip
    HubSpot's rate limit under normal operation, not just at fault."""
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    _mock(provider, lambda r: httpx.Response(429, json={"message": "rate limited"}))
    with pytest.raises(IntegrationError) as exc_info:
        await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email="x@example.com"))
    assert not isinstance(exc_info.value, PermanentIntegrationError)


# ── activities ───────────────────────────────────────────────────────────


@requires_services
async def test_activity_shape_and_association(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(201, json={"id": "call-1"}))

    occurred = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    activity_id = await provider.log_activity(
        tenant_id,
        ActivityRecord(
            contact_external_id="contact-1",
            summary="Caller booked a cleaning for Tuesday.",
            occurred_at=occurred,
            duration_seconds=90,
        ),
    )

    assert activity_id == "call-1"
    body = json.loads(seen[0].content)
    assert body["properties"]["hs_call_body"] == "Caller booked a cleaning for Tuesday."
    assert body["properties"]["hs_timestamp"] == int(occurred.timestamp() * 1000)
    assert body["properties"]["hs_call_duration"] == 90_000
    assert body["associations"][0]["to"]["id"] == "contact-1"


# ── deals ────────────────────────────────────────────────────────────────


@requires_services
async def test_deal_shape_and_association(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    seen = _mock(provider, lambda r: httpx.Response(201, json={"id": "deal-1"}))

    deal_id = await provider.create_deal(
        tenant_id,
        DealRecord(
            contact_external_id="contact-1",
            title="Cleaning — Dana Lee",
            stage="appointmentscheduled",
            properties={"pipeline": "sales"},
        ),
    )

    assert deal_id == "deal-1"
    body = json.loads(seen[0].content)
    assert body["properties"]["dealname"] == "Cleaning — Dana Lee"
    assert body["properties"]["dealstage"] == "appointmentscheduled"
    assert body["properties"]["pipeline"] == "sales"
    assert body["associations"][0]["to"]["id"] == "contact-1"
    assert "amount" not in body["properties"]


@requires_services
async def test_deal_permanent_error(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings())
    _mock(provider, lambda r: httpx.Response(404, json={"message": "pipeline not found"}))
    with pytest.raises(PermanentIntegrationError, match="deal create rejected"):
        await provider.create_deal(
            tenant_id,
            DealRecord(contact_external_id="c1", title="t", stage="s"),
        )


# ── token refresh ────────────────────────────────────────────────────────


@requires_services
async def test_expired_token_is_refreshed_before_use(expired_tenant) -> None:
    tenant_id, _ = expired_tenant
    provider = HubSpotCRMProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.hubapi.com" and request.url.path == "/oauth/v1/token":
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 1800})
        if request.url.path == "/crm/v3/objects/contacts/search":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "contact-new"})

    seen = _mock(provider, handler)
    await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email=None))

    assert seen[0].url.path == "/oauth/v1/token"
    refresh_body = dict(httpx.QueryParams(seen[0].content.decode()))
    assert refresh_body["grant_type"] == "refresh_token"
    assert refresh_body["refresh_token"] == "refresh-token-1"
    # Subsequent calls use the new token.
    assert seen[1].headers["Authorization"] == "Bearer fresh-token"


@requires_services
async def test_refreshed_token_is_persisted(expired_tenant) -> None:
    tenant_id, _ = expired_tenant
    provider = HubSpotCRMProvider(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v1/token":
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 1800})
        if request.url.path == "/crm/v3/objects/contacts/search":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "contact-new"})

    seen = _mock(provider, handler)
    await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email=None))
    await provider.upsert_contact(tenant_id, ContactRecord(phone="+2", email=None))

    refreshes = [r for r in seen if r.url.path == "/oauth/v1/token"]
    assert len(refreshes) == 1


@requires_services
async def test_revoked_grant_is_marked(expired_tenant) -> None:
    tenant_id, integration_id = expired_tenant
    provider = HubSpotCRMProvider(_settings())
    _mock(provider, lambda r: httpx.Response(400, json={"message": "invalid_grant"}))

    with pytest.raises(PermanentIntegrationError, match="token refresh failed"):
        await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email=None))

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
async def test_no_integration_is_a_permanent_error(connected_tenant) -> None:
    """Not worth retrying: connecting HubSpot is a one-time setup step, not a
    transient outage. The 30-second confirmations worker won't fix it."""
    provider = HubSpotCRMProvider(_settings())
    with pytest.raises(PermanentIntegrationError, match="no connected HubSpot"):
        await provider.upsert_contact(uuid.uuid4(), ContactRecord(phone="+1", email=None))


@requires_services
async def test_missing_encryption_key_fails_at_use_not_at_startup(connected_tenant) -> None:
    tenant_id, _ = connected_tenant
    provider = HubSpotCRMProvider(_settings(credentials_encryption_key=""))  # constructs fine
    with pytest.raises(Exception, match="CREDENTIALS_ENCRYPTION_KEY"):
        await provider.upsert_contact(tenant_id, ContactRecord(phone="+1", email=None))
