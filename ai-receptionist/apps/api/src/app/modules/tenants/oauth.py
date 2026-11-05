"""OAuth authorization-code flow for connecting a tenant's Google Calendar
or HubSpot account — the one-time interactive "Connect" step that produces
the `integrations` row `GoogleCalendarProvider`/`HubSpotCRMProvider` read
from on every subsequent call. Deliberately separate from those adapters:
they own the runtime refresh-and-call loop, this owns the browser round-trip
that bootstraps it.

## Why `state` carries the tenant, signed

The callback (`oauth_routes.callback`) is a redirect the *provider* sends the
browser to — there is no Authorization header to read, and no session to
consult. The `state` parameter is where an OAuth flow is allowed to carry
identity, so it holds a JWT (via `core.security.encode_jwt`, `typ:
"oauth_state"`) naming the tenant and provider. Signed and short-lived
(`OAUTH_STATE_TTL`) so a captured/replayed state either fails the signature
check or has already expired; distinguished by `typ` from a login access
token so one can never be replayed as the other (see security.py's
module docstring).

## Token URLs are imported, not duplicated

`google.TOKEN_URL` / `hubspot.TOKEN_URL` are the exact endpoints the runtime
adapters already refresh against — importing them here means a URL can't
drift between "where we get the first token" and "where we refresh it".
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.errors import IntegrationError, PermanentIntegrationError, ValidationFailedError
from app.core.security import decode_jwt, encode_jwt
from app.modules.crm.providers.hubspot import TOKEN_URL as HUBSPOT_TOKEN_URL
from app.modules.scheduling.providers.google import TOKEN_URL as GOOGLE_TOKEN_URL

Provider = Literal["google_calendar", "hubspot"]
PROVIDERS: tuple[Provider, ...] = ("google_calendar", "hubspot")

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"
HUBSPOT_AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
HUBSPOT_SCOPES = (
    "crm.objects.contacts.read crm.objects.contacts.write "
    "crm.objects.deals.read crm.objects.deals.write "
    "crm.objects.calls.read crm.objects.calls.write"
)


def _validate_provider(provider: str) -> Provider:
    if provider not in PROVIDERS:
        raise ValidationFailedError(f"unknown integration provider {provider!r}")
    return provider


def _redirect_uri(settings: Settings, provider: Provider) -> str:
    if not settings.public_webhook_base_url:
        raise IntegrationError(
            "PUBLIC_WEBHOOK_BASE_URL is not set — OAuth providers need a "
            "reachable redirect URI to send the caller back to"
        )
    return f"{settings.public_webhook_base_url}/api/v1/integrations/{provider}/callback"


def build_authorize_url(*, provider: str, tenant_id: uuid.UUID, settings: Settings) -> str:
    provider = _validate_provider(provider)
    state = encode_jwt(
        {"tenant_id": str(tenant_id), "provider": provider, "typ": "oauth_state"},
        secret_key=settings.secret_key,
        expires_in_minutes=settings.oauth_state_ttl_minutes,
    )
    redirect_uri = _redirect_uri(settings, provider)

    if provider == "google_calendar":
        if not settings.google_oauth_client_id:
            raise IntegrationError("GOOGLE_OAUTH_CLIENT_ID is not configured")
        params = {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            # Google only returns a refresh_token on the first consent grant
            # unless forced — without this, a second "Connect" click (e.g.
            # after a revoke) silently leaves the tenant without one.
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    if not settings.hubspot_client_id:
        raise IntegrationError("HUBSPOT_CLIENT_ID is not configured")
    params = {
        "client_id": settings.hubspot_client_id,
        "redirect_uri": redirect_uri,
        "scope": HUBSPOT_SCOPES,
        "state": state,
    }
    return f"{HUBSPOT_AUTHORIZE_URL}?{urlencode(params)}"


def decode_state(state: str, *, settings: Settings) -> tuple[uuid.UUID, Provider]:
    payload = decode_jwt(state, secret_key=settings.secret_key)
    if payload.get("typ") != "oauth_state":
        raise ValidationFailedError("wrong token type in OAuth state")
    provider = _validate_provider(payload["provider"])
    return uuid.UUID(payload["tenant_id"]), provider


async def exchange_code(
    *,
    provider: Provider,
    code: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Trade an authorization code for tokens; return the exact credentials
    shape `GoogleCalendarProvider`/`HubSpotCRMProvider` expect to decrypt —
    {access_token, refresh_token, expiry} (Google's dict also needs no
    `calendar_id`; the adapter defaults that to "primary" when absent).

    `client` is injectable so tests can pass one backed by
    `httpx.MockTransport` — the same seam every vendor adapter's `_http()`
    provides, just as a parameter instead of a method since this is a free
    function, not a class with a runtime-long lifetime.
    """
    redirect_uri = _redirect_uri(settings, provider)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        if provider == "google_calendar":
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": settings.google_oauth_client_id,
                    "client_secret": settings.google_oauth_client_secret,
                },
            )
        else:
            response = await client.post(
                HUBSPOT_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": settings.hubspot_client_id,
                    "client_secret": settings.hubspot_client_secret,
                },
            )
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 400:
        # A code exchange is one-shot and user-interactive — retrying it
        # automatically can't help (the code is already spent, or the
        # caller mistyped consent), so this is permanent, not transient.
        raise PermanentIntegrationError(
            f"{provider} token exchange failed ({response.status_code}): {response.text[:200]}"
        )

    payload = response.json()
    if "refresh_token" not in payload:
        # Google omits this on a re-consent that didn't actually re-prompt;
        # HubSpot always returns one. Either way, a credential set with no
        # refresh token silently stops working the moment the access token
        # expires — better to fail the connect flow now than the first
        # scheduling/CRM call after the access token dies.
        raise PermanentIntegrationError(
            f"{provider} did not return a refresh token — reconnect and approve the consent screen"
        )

    return {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expiry": (
            datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
        ).isoformat(),
    }
