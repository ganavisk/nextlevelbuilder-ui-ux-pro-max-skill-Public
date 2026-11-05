import hashlib
import hmac

import pytest
from cryptography.fernet import Fernet

from app.core.errors import WebhookSignatureError
from app.core.security import CredentialCipher, verify_webhook_signature


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_valid_signature_passes() -> None:
    payload = b'{"event":"call_started"}'
    verify_webhook_signature(payload, _sign(payload, "s3cret"), "s3cret")


def test_tampered_payload_rejected() -> None:
    signature = _sign(b'{"event":"call_started"}', "s3cret")
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(b'{"event":"call_ended"}', signature, "s3cret")


def test_missing_secret_rejected() -> None:
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(b"{}", "anything", "")


def test_credential_cipher_round_trip() -> None:
    cipher = CredentialCipher(Fernet.generate_key().decode())
    token = '{"access_token":"ya29.example"}'
    assert cipher.decrypt(cipher.encrypt(token)) == token
