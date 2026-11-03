"""Google Calendar adapter — the only file that knows Google's wire format.

Per-tenant OAuth credentials live Fernet-encrypted in `integrations`; this
adapter decrypts, refreshes an expired access token, and calls the Calendar
v3 REST API directly (freeBusy for availability, events.insert for booking).

Deliberately no google-api-python-client: that SDK is sync-only and heavy,
and we need exactly two endpoints on the async path.

Not yet contract-validated against live Google — covered by fixture/mock
tests here; verify during the M3 live-booking test.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import table_of, tenant_session
from app.core.errors import IntegrationError
from app.core.logging import get_logger
from app.core.security import CredentialCipher
from app.modules.scheduling.providers.base import (
    BookingRequest,
    BookingResult,
    CalendarProvider,
    TimeSlot,
)
from app.modules.tenants.models import Integration

logger = get_logger(__name__)

CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Refresh a little early rather than racing the expiry on a live call.
EXPIRY_SKEW = timedelta(seconds=60)


class GoogleCalendarProvider(CalendarProvider):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cipher_instance: CredentialCipher | None = None

    # ── credentials ────────────────────────────────────────────────────────

    @property
    def _cipher(self) -> CredentialCipher:
        """Built on first use, not at construction. The provider is
        instantiated during app startup for every deployment, but only ones
        that actually book need an encryption key — constructing it eagerly
        would turn a missing optional secret into a failure to boot."""
        if self._cipher_instance is None:
            self._cipher_instance = CredentialCipher(self._settings.credentials_encryption_key)
        return self._cipher_instance

    async def _credentials(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(Integration).where(
                        Integration.tenant_id == tenant_id,
                        Integration.provider == "google_calendar",
                    )
                )
            ).scalar_one_or_none()
            if row is None or row.status != "connected":
                raise IntegrationError(
                    f"tenant {tenant_id} has no connected Google Calendar integration"
                )
            credentials: dict[str, Any] = json.loads(
                self._cipher.decrypt(row.credentials_encrypted)
            )
            credentials.setdefault("calendar_id", row.config.get("calendar_id", "primary"))
            row_id = row.id

        if self._is_expired(credentials):
            credentials = await self._refresh(tenant_id, row_id, credentials)
        return credentials

    @staticmethod
    def _is_expired(credentials: dict[str, Any]) -> bool:
        expiry = credentials.get("expiry")
        if not expiry:
            return False
        return datetime.fromisoformat(expiry) - EXPIRY_SKEW <= datetime.now(UTC)

    async def _refresh(
        self, tenant_id: uuid.UUID, integration_id: uuid.UUID, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise IntegrationError("Google Calendar access token expired and no refresh token")

        async with self._http() as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._settings.google_oauth_client_id,
                    "client_secret": self._settings.google_oauth_client_secret,
                },
            )
        if response.status_code >= 400:
            # A revoked grant is permanent — mark it so the dashboard can
            # prompt a reconnect instead of retrying forever.
            await self._mark_status(tenant_id, integration_id, "revoked")
            raise IntegrationError(f"Google token refresh failed: {response.status_code}")

        payload = response.json()
        credentials["access_token"] = payload["access_token"]
        credentials["expiry"] = (
            datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
        ).isoformat()

        async with tenant_session(tenant_id) as session:
            await session.execute(
                table_of(Integration)
                .update()
                .where(Integration.id == integration_id)
                .values(credentials_encrypted=self._cipher.encrypt(json.dumps(credentials)))
            )
        logger.info("google_token_refreshed", tenant_id=str(tenant_id))
        return credentials

    async def _mark_status(
        self, tenant_id: uuid.UUID, integration_id: uuid.UUID, status: str
    ) -> None:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                table_of(Integration)
                .update()
                .where(Integration.id == integration_id)
                .values(status=status)
            )

    def _http(self) -> httpx.AsyncClient:
        # Tight timeout: this sits on the in-call hot path, where a slow
        # calendar must degrade to "let me take a message" rather than
        # holding the caller in silence (Phase 1 §3.3).
        return httpx.AsyncClient(timeout=5)

    # ── CalendarProvider ───────────────────────────────────────────────────

    async def list_busy_periods(
        self, tenant_id: uuid.UUID, window_start: datetime, window_end: datetime
    ) -> list[TimeSlot]:
        credentials = await self._credentials(tenant_id)
        async with self._http() as client:
            response = await client.post(
                f"{CALENDAR_BASE_URL}/freeBusy",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
                json={
                    "timeMin": window_start.astimezone(UTC).isoformat(),
                    "timeMax": window_end.astimezone(UTC).isoformat(),
                    "items": [{"id": credentials["calendar_id"]}],
                },
            )
        if response.status_code >= 400:
            raise IntegrationError(f"Google freeBusy failed: {response.status_code}")

        calendars = response.json().get("calendars", {})
        entry = calendars.get(credentials["calendar_id"], {})
        if entry.get("errors"):
            raise IntegrationError(f"Google freeBusy error: {entry['errors']}")
        return [
            TimeSlot(
                start=datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                end=datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
            for b in entry.get("busy", [])
        ]

    async def book(self, request: BookingRequest) -> BookingResult:
        credentials = await self._credentials(request.tenant_id)
        attendees = [{"email": request.attendee_email}] if request.attendee_email else []
        body = {
            "summary": request.summary,
            "description": (
                f"Booked by the AI receptionist.\n"
                f"Caller: {request.attendee_name} ({request.attendee_phone})"
            ),
            "start": {"dateTime": request.slot.start.astimezone(UTC).isoformat()},
            "end": {"dateTime": request.slot.end.astimezone(UTC).isoformat()},
            "attendees": attendees,
            # Transparent events don't count in free/busy, so a multi-bay shop's
            # own bookings never make the calendar look fully booked. Its real
            # blocks (closures, staff time off) stay opaque and still apply.
            "transparency": "opaque" if request.blocks_time else "transparent",
        }
        if request.idempotency_key:
            # Google dedupes on event id; a repeated booking returns the
            # existing event instead of creating a duplicate.
            body["id"] = _google_event_id(request.idempotency_key)

        async with self._http() as client:
            response = await client.post(
                f"{CALENDAR_BASE_URL}/calendars/{credentials['calendar_id']}/events",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
                json=body,
            )
        if response.status_code == 409:
            # Duplicate event id — the same booking already exists.
            return BookingResult(
                external_event_id=str(body.get("id", "")), slot=request.slot, confirmed=True
            )
        if response.status_code >= 400:
            raise IntegrationError(f"Google event insert failed: {response.status_code}")

        return BookingResult(
            external_event_id=str(response.json()["id"]), slot=request.slot, confirmed=True
        )

    async def cancel(self, tenant_id: uuid.UUID, external_event_id: str) -> None:
        credentials = await self._credentials(tenant_id)
        async with self._http() as client:
            response = await client.delete(
                f"{CALENDAR_BASE_URL}/calendars/{credentials['calendar_id']}"
                f"/events/{external_event_id}",
                headers={"Authorization": f"Bearer {credentials['access_token']}"},
            )
        # 410 Gone means it was already deleted — the desired end state.
        if response.status_code >= 400 and response.status_code not in (404, 410):
            raise IntegrationError(f"Google event delete failed: {response.status_code}")


def _google_event_id(idempotency_key: str) -> str:
    """Google event ids allow only lowercase a-v and digits 0-9, length 5-1024."""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex
    table = str.maketrans("wxyz", "0123")
    return digest.translate(table)
