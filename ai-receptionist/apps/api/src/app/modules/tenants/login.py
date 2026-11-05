"""Dashboard login: resolve tenant by slug, verify password, issue a JWT.

Deliberately thin — JWT signing, password hashing, and the authentication
error hierarchy all live in `core.security`/`core.errors`; this module only
wires tenant+user resolution to them for the one login use case.
"""

import hashlib
from dataclasses import dataclass
from functools import cache

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.errors import AuthenticationError, RateLimitedError, TenantNotFoundError
from app.core.logging import get_logger
from app.core.ratelimit import hit, refund, reset
from app.core.security import encode_jwt, hash_password, verify_password
from app.modules.tenants.models import Tenant, User
from app.modules.tenants.repository import get_tenant_by_slug, get_user_by_email

logger = get_logger(__name__)

INVALID = "invalid email or password"


@cache
def _decoy_hash() -> str:
    # A real bcrypt hash of a throwaway value, computed once. Verifying
    # against it costs the same as verifying a real user's hash.
    return hash_password("decoy-password-never-matches")


def _burn_password_check(password: str) -> None:
    """Spend the time a real password check would, then let the caller fail.

    Without this, an unknown business or email returns in microseconds while a
    known one spends ~200ms in bcrypt, so response time alone tells an
    attacker which accounts exist — undoing the identical-error-body
    protection."""
    verify_password(password, _decoy_hash())


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    tenant: Tenant
    user: User


def _account_key(tenant_slug: str, email: str) -> str:
    # Hashed: the raw inputs are attacker-controlled and unbounded in length.
    digest = hashlib.sha256(f"{tenant_slug.strip().lower()}\0{email.strip().lower()}".encode())
    return f"rl:login:acct:{digest.hexdigest()}"


async def _throttle(redis: Redis, *, account_key: str, ip_key: str, settings: Settings) -> None:
    """Count this attempt against the account and the client IP *before*
    looking at the password, and refuse once either is over its limit.

    Incrementing first (rather than checking, then counting failures) is what
    makes a burst of parallel guesses count: every request is numbered
    atomically, so request #11 is refused no matter how many are in flight.
    The check also precedes any lookup, so a locked-out caller cannot learn
    whether the password was right, or whether the account exists.
    """
    try:
        acct_count, acct_ttl = await hit(redis, account_key, settings.login_window_seconds)
        ip_count, ip_ttl = await hit(redis, ip_key, settings.login_window_seconds)
    except RedisError:
        # Fail open: the dashboard is not on the call path, but a Redis blip
        # should not also lock every user out of their own account.
        logger.warning("login_throttle_unavailable")
        return
    if acct_count > settings.login_max_attempts_per_account or (
        ip_count > settings.login_max_attempts_per_ip
    ):
        retry_after = max(
            acct_ttl if acct_count > settings.login_max_attempts_per_account else 0,
            ip_ttl if ip_count > settings.login_max_attempts_per_ip else 0,
        )
        logger.warning("login_throttled", retry_after_seconds=retry_after)
        raise RateLimitedError(
            "too many sign-in attempts; try again later", retry_after_seconds=retry_after
        )


async def login(
    *,
    tenant_slug: str,
    email: str,
    password: str,
    settings: Settings,
    redis: Redis | None = None,
    client_ip: str = "unknown",
) -> LoginResult:
    account_key = _account_key(tenant_slug, email)
    ip_key = f"rl:login:ip:{client_ip}"
    if redis is not None:
        await _throttle(redis, account_key=account_key, ip_key=ip_key, settings=settings)

    result = await _authenticate(
        tenant_slug=tenant_slug, email=email, password=password, settings=settings
    )

    if redis is not None:
        # A successful sign-in clears the account's failures and gives the IP
        # its attempt back, so a shared office address is only ever charged
        # for failures.
        try:
            await reset(redis, account_key)
            await refund(redis, ip_key)
        except RedisError:
            logger.warning("login_throttle_unavailable")
    return result


async def _authenticate(
    *, tenant_slug: str, email: str, password: str, settings: Settings
) -> LoginResult:
    try:
        tenant = await get_tenant_by_slug(tenant_slug)
    except TenantNotFoundError as exc:
        _burn_password_check(password)
        # Folded into the same error as "wrong password": whether a given
        # business slug exists at all shouldn't be distinguishable from a
        # failed login either, by the same email-enumeration logic
        # `get_user_by_email` already applies one level down.
        raise AuthenticationError(INVALID) from exc

    try:
        user = await get_user_by_email(tenant.id, email)
    except AuthenticationError:
        _burn_password_check(password)
        raise
    if not verify_password(password, user.password_hash):
        raise AuthenticationError(INVALID)

    token = encode_jwt(
        {
            "sub": str(user.id),
            "tenant_id": str(tenant.id),
            "role": user.role,
            "email": user.email,
            "typ": "access",
        },
        secret_key=settings.secret_key,
        expires_in_minutes=settings.access_token_ttl_minutes,
    )
    return LoginResult(access_token=token, tenant=tenant, user=user)
