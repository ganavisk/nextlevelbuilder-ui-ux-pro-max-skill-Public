"""Twilio adapter — transactional SMS.

Compliance is a design constraint, not a nicety: SMS goes only to callers who
just spoke to us and booked something (transactional), the body carries an
opt-out, and Twilio's Messaging Service handles STOP handling and number
selection. See the notifications README for the TCPA position.
"""

import httpx

from app.core.config import Settings
from app.core.errors import IntegrationError
from app.modules.notifications.providers.base import SMSProvider

TWILIO_BASE_URL = "https://api.twilio.com/2010-04-01"


class TwilioSMSProvider(SMSProvider):
    def __init__(self, settings: Settings) -> None:
        self._account_sid = settings.twilio_account_sid
        self._auth_token = settings.twilio_auth_token
        self._messaging_service_sid = settings.twilio_messaging_service_sid

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, auth=(self._account_sid, self._auth_token))

    async def send(self, *, to: str, body: str, idempotency_key: str) -> str:
        if not (self._account_sid and self._auth_token and self._messaging_service_sid):
            raise IntegrationError("Twilio SMS credentials are not fully configured")
        async with self._client() as client:
            response = await client.post(
                f"{TWILIO_BASE_URL}/Accounts/{self._account_sid}/Messages.json",
                data={
                    "To": to,
                    "Body": body,
                    "MessagingServiceSid": self._messaging_service_sid,
                },
            )
        if response.status_code >= 400:
            raise IntegrationError(
                f"Twilio send failed ({response.status_code}): {response.text[:200]}"
            )
        return str(response.json().get("sid", ""))
