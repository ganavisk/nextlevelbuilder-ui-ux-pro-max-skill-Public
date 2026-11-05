"""Integration connect flow: list status, start the OAuth dance, receive the
provider's callback. Split from `routes.py` because the callback has a
fundamentally different trust model — the provider redirects the browser
here with no Authorization header at all, so it's authenticated by the
signed `state` parameter (see `oauth.py`) instead of `core.auth`.

`/connect` returns the authorize URL as JSON rather than issuing the
redirect itself, so the dashboard's own server can fetch it with a Bearer
token (a cross-origin browser navigation can't carry one) and then perform
the actual redirect from a first-party context. See apps/dashboard's
`src/app/api/integrations/[provider]/connect/route.ts`.
"""

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.core.auth import AuthContext, get_current_user, require_role
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import CredentialCipher
from app.modules.tenants.oauth import PROVIDERS, build_authorize_url, decode_state, exchange_code
from app.modules.tenants.repository import list_integrations, upsert_integration
from app.modules.tenants.schemas import ConnectUrlResponse, IntegrationSummary

router = APIRouter()


class OAuthDeniedError(AppError):
    """The caller declined consent on the provider's screen — an expected,
    routine outcome, not a bug. A distinct `code` so the dashboard can show
    "you'll need to approve access to connect" rather than a generic error."""

    code = "oauth_denied"


class OAuthCallbackInvalidError(AppError):
    """The callback itself is malformed or tampered with — missing
    code/state, or a state whose provider doesn't match the URL. Not
    something a legitimate OAuth redirect ever produces."""

    code = "oauth_callback_invalid"


@router.get("/integrations", response_model=list[IntegrationSummary])
async def list_my_integrations(
    auth: AuthContext = Depends(get_current_user),
) -> list[IntegrationSummary]:
    by_provider = {row.provider: row for row in await list_integrations(auth.tenant_id)}
    return [
        IntegrationSummary(
            provider=provider,
            status=by_provider[provider].status if provider in by_provider else "not_connected",
            connected=by_provider.get(provider) is not None
            and by_provider[provider].status == "connected",
        )
        for provider in PROVIDERS
    ]


@router.get("/integrations/{provider}/connect", response_model=ConnectUrlResponse)
async def connect(
    provider: str, request: Request, auth: AuthContext = Depends(require_role("admin"))
) -> ConnectUrlResponse:
    settings: Settings = request.app.state.settings
    url = build_authorize_url(provider=provider, tenant_id=auth.tenant_id, settings=settings)
    return ConnectUrlResponse(authorize_url=url)


@router.get("/integrations/{provider}/callback")
async def callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    # Every exit from here is a browser redirect, not a JSON error response —
    # this endpoint is reached by full-page navigation from Google/HubSpot,
    # never fetched programmatically, so a JSON body would just render as
    # raw text in the user's browser instead of the dashboard's error UI.
    try:
        if error:
            raise OAuthDeniedError(f"provider denied the request: {error}")
        if not code or not state:
            raise OAuthCallbackInvalidError("missing code or state")

        tenant_id, state_provider = decode_state(state, settings=settings)
        if state_provider != provider:
            raise OAuthCallbackInvalidError("state does not match the requested provider")

        credentials = await exchange_code(provider=state_provider, code=code, settings=settings)
        cipher = CredentialCipher(settings.credentials_encryption_key)
        await upsert_integration(tenant_id, state_provider, cipher.encrypt(json.dumps(credentials)))
    except AppError as exc:
        return RedirectResponse(f"{settings.dashboard_base_url}/settings?error={exc.code}")

    return RedirectResponse(f"{settings.dashboard_base_url}/settings?connected={provider}")
