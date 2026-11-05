"""Dashboard authentication: bearer JWT → AuthContext.

The dashboard's own Next.js server holds the httpOnly session cookie and
forwards it here as a `Bearer` token on every server-to-server call (see
`apps/dashboard/src/lib/api.ts`) — this API only ever sees Authorization
headers, never cookies. That keeps cross-origin cookie handling entirely on
the dashboard's side of the boundary; this API's CORS config only needs to
allow the header, not credentials.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import decode_jwt

# auto_error=False: a missing header should raise our own AuthenticationError
# (uniform error shape via register_error_handlers), not FastAPI's default
# HTTPException-shaped 403.
_bearer_scheme = HTTPBearer(auto_error=False)

# A flat hierarchy — owner > admin > viewer — checked by rank, not identity,
# so a new role only ever needs one line here, not a change at every call site.
ROLE_RANK = {"viewer": 0, "admin": 1, "owner": 2}


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    email: str


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthContext:
    if credentials is None:
        raise AuthenticationError("missing bearer token")

    settings: Settings = request.app.state.settings
    payload = decode_jwt(credentials.credentials, secret_key=settings.secret_key)
    if payload.get("typ") != "access":
        # Blocks a stolen/leaked OAuth-state token (same secret, same
        # encode_jwt/decode_jwt) from being replayed as a login session.
        raise AuthenticationError("wrong token type")

    try:
        return AuthContext(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            role=payload["role"],
            email=payload["email"],
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("malformed token claims") from exc


def require_role(minimum: str) -> Callable[..., Awaitable[AuthContext]]:
    """Dependency factory: `Depends(require_role("admin"))` lets admin/owner
    through and rejects viewer with 403. `Depends(get_current_user)` alone is
    the "any signed-in user" case — every authenticated route needs one or
    the other, never neither.
    """

    async def _check(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
        if ROLE_RANK.get(auth.role, -1) < ROLE_RANK[minimum]:
            raise AuthorizationError(f"requires {minimum} role or higher")
        return auth

    return _check
