"""Security primitives: webhook signatures, credential encryption, dashboard
auth (JWTs + password hashing).

- Voice-vendor webhooks are HMAC-signed; every inbound webhook must pass
  `verify_webhook_signature` before any processing (rejecting unsigned or
  tampered payloads at the edge).
- Per-tenant integration credentials (Google/HubSpot OAuth tokens) are
  encrypted at rest with Fernet (AES-128-CBC + HMAC); the key lives only in
  the environment, never in the database.
- Dashboard sessions are stateless JWTs (HS256, `secret_key`). The same
  `encode_jwt`/`decode_jwt` pair also signs the short-lived OAuth `state`
  parameter in the integrations connect flow — a different `typ` claim on
  each keeps a leaked login token from being replayed as OAuth state, and
  vice versa (`core.auth` / `tenants.oauth` check `typ` before trusting
  anything else in the payload).
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import AppError, AuthenticationError, WebhookSignatureError


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> None:
    """Constant-time HMAC-SHA256 verification. Raises on mismatch."""
    if not secret:
        raise WebhookSignatureError("webhook secret not configured")
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise WebhookSignatureError("signature mismatch")


class CredentialCipher:
    """Encrypts/decrypts per-tenant integration credentials at rest."""

    def __init__(self, key: str) -> None:
        if not key:
            raise AppError("CREDENTIALS_ENCRYPTION_KEY is not configured")
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise AppError("credential decryption failed — key rotated?") from exc


JWT_ALGORITHM = "HS256"


def encode_jwt(claims: dict[str, Any], *, secret_key: str, expires_in_minutes: int) -> str:
    """Sign `claims` plus standard `iat`/`exp`. Callers add their own `typ`
    claim (see module docstring) — this function doesn't know or care what
    kind of token it's signing."""
    now = datetime.now(UTC)
    payload = {
        **claims,
        "iat": now,
        "exp": now + timedelta(minutes=expires_in_minutes),
    }
    return jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str, *, secret_key: str) -> dict[str, Any]:
    """Verify signature and expiry; raise AuthenticationError on anything
    wrong rather than leaking which check failed (expired vs. tampered vs.
    malformed all look the same to a caller with no reason to know)."""
    try:
        return jwt.decode(token, secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthenticationError("invalid or expired token") from exc


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """False on any malformed hash (e.g. the '' default on a user who never
    set a password) rather than raising — bcrypt.checkpw raises ValueError
    on a hash that isn't its own format, and "this login attempt fails" is
    the correct outcome either way."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False
