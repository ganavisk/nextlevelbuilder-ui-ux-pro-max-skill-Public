"""JWT signing/verification, password hashing, and role-rank checking.
Pure unit tests — no database, no FastAPI app."""

import time
import uuid

import jwt
import pytest

from app.core.auth import ROLE_RANK, AuthContext, require_role
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import decode_jwt, encode_jwt, hash_password, verify_password

SECRET = "test-secret-at-least-32-bytes-long!!"


# ── JWT ──────────────────────────────────────────────────────────────────


def test_round_trip_preserves_claims() -> None:
    token = encode_jwt({"sub": "abc", "typ": "access"}, secret_key=SECRET, expires_in_minutes=5)
    payload = decode_jwt(token, secret_key=SECRET)
    assert payload["sub"] == "abc"
    assert payload["typ"] == "access"
    assert "iat" in payload and "exp" in payload


def test_wrong_secret_is_rejected() -> None:
    token = encode_jwt({"sub": "abc"}, secret_key=SECRET, expires_in_minutes=5)
    with pytest.raises(AuthenticationError):
        decode_jwt(token, secret_key="a-completely-different-secret-value")


def test_expired_token_is_rejected() -> None:
    # Sign with a past expiry directly (bypassing encode_jwt's now+ttl) so
    # the test doesn't need to sleep.
    payload = {"sub": "abc", "iat": time.time() - 120, "exp": time.time() - 60}
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    with pytest.raises(AuthenticationError):
        decode_jwt(token, secret_key=SECRET)


def test_tampered_payload_is_rejected() -> None:
    token = encode_jwt({"sub": "abc"}, secret_key=SECRET, expires_in_minutes=5)
    header, payload, sig = token.split(".")
    # Flip a character in the payload segment without re-signing.
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(AuthenticationError):
        decode_jwt(f"{header}.{tampered_payload}.{sig}", secret_key=SECRET)


def test_malformed_token_is_rejected() -> None:
    with pytest.raises(AuthenticationError):
        decode_jwt("not-a-jwt-at-all", secret_key=SECRET)


# ── password hashing ─────────────────────────────────────────────────────


def test_correct_password_verifies() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True


def test_wrong_password_fails() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("wrong password", h) is False


def test_empty_hash_never_verifies() -> None:
    """The `''` default on a user who never set a password must fail every
    login attempt, not raise and not (worse) accidentally accept anything."""
    assert verify_password("anything", "") is False


def test_malformed_hash_fails_closed_not_raises() -> None:
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_hashes_are_salted() -> None:
    """Two hashes of the same password must differ — otherwise a leaked
    hash table trivially reveals which users share a password."""
    assert hash_password("same-password") != hash_password("same-password")


# ── role ranking ─────────────────────────────────────────────────────────


def test_role_rank_is_strictly_ordered() -> None:
    assert ROLE_RANK["viewer"] < ROLE_RANK["admin"] < ROLE_RANK["owner"]


async def test_require_role_allows_equal_and_higher() -> None:
    check = require_role("admin")
    admin = AuthContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="admin", email="a@x.com")
    owner = AuthContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="owner", email="o@x.com")
    assert await check(admin) is admin
    assert await check(owner) is owner


async def test_require_role_rejects_lower() -> None:
    check = require_role("admin")
    viewer = AuthContext(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="viewer", email="v@x.com"
    )
    with pytest.raises(AuthorizationError):
        await check(viewer)


async def test_require_role_rejects_unknown_role() -> None:
    """A role that isn't in ROLE_RANK at all (e.g. a typo written straight
    to the database) must fail closed, not raise a KeyError that turns into
    an unhandled 500."""
    check = require_role("viewer")
    bogus = AuthContext(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="superadmin", email="s@x.com"
    )
    with pytest.raises(AuthorizationError):
        await check(bogus)
