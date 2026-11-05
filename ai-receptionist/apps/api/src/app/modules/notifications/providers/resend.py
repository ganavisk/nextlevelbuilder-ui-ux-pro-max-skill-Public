"""Resend adapter — transactional email.

Resend accepts an Idempotency-Key header, so a retried job reuses the
original send rather than emailing the customer twice.
"""

import httpx

from app.core.config import Settings
from app.core.errors import IntegrationError
from app.modules.notifications.providers.base import EmailProvider

RESEND_URL = "https://api.resend.com/emails"


class ResendEmailProvider(EmailProvider):
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.resend_api_key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15)

    async def send(
        self, *, to: str, subject: str, html: str, from_address: str, idempotency_key: str
    ) -> str:
        if not self._api_key:
            raise IntegrationError("RESEND_API_KEY is not configured")
        async with self._client() as client:
            response = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": idempotency_key,
                },
                json={"from": from_address, "to": [to], "subject": subject, "html": html},
            )
        if response.status_code >= 400:
            raise IntegrationError(
                f"Resend send failed ({response.status_code}): {response.text[:200]}"
            )
        return str(response.json().get("id", ""))
