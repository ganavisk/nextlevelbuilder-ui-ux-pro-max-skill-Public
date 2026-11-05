"""HubSpot adapter — the only file that knows HubSpot's wire format.

Per-tenant OAuth credentials live Fernet-encrypted in `integrations`; this
adapter decrypts, refreshes an expired access token, and calls the CRM v3
REST API directly. No hubspot-api-client: it is sync-only and we need four
endpoints.

## Contact identity

HubSpot deduplicates contacts on email, and only on email. Most of our
callers give a phone number and no email, so matching has to be done
explicitly:

    email present  -> POST /objects/contacts, 409 means "exists" -> PATCH
    email absent   -> search by phone -> PATCH the hit, else POST

Getting this wrong creates a duplicate contact per call, which is the single
most visible way to damage a customer's CRM.

## Error classification

4xx that will never succeed on retry (400/401/403/404/422) raise
`PermanentIntegrationError` so the queue dead-letters immediately instead of
retrying five times. 429 and 5xx raise `IntegrationError` and are retried
with backoff.

Not yet contract-validated against live HubSpot — covered by mock-transport
tests here; verify during the M4 live-sync test.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import table_of, tenant_session
from app.core.errors import IntegrationError, PermanentIntegrationError
from app.core.logging import get_logger
from app.core.security import CredentialCipher
from app.modules.crm.providers.base import (
    ActivityRecord,
    ContactRecord,
    CRMProvider,
    DealRecord,
)
from app.modules.tenants.models import Integration

logger = get_logger(__name__)

API_BASE_URL = "https://api.hubapi.com"
TOKEN_URL = f"{API_BASE_URL}/oauth/v1/token"
EXPIRY_SKEW = timedelta(seconds=60)

# Statuses that mean "this request is wrong and will stay wrong".
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 422})

# HubSpot association type ids (v4 association API). These are HubSpot's
# defined constants, not arbitrary numbers.
ASSOC_CALL_TO_CONTACT = 194
ASSOC_DEAL_TO_CONTACT = 3


class HubSpotCRMProvider(CRMProvider):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cipher_instance: CredentialCipher | None = None

    # ── credentials ────────────────────────────────────────────────────────

    @property
    def _cipher(self) -> CredentialCipher:
        """Built on first use: the provider is constructed during app startup
        for every deployment, but only ones that sync a CRM need a key."""
        if self._cipher_instance is None:
            self._cipher_instance = CredentialCipher(self._settings.credentials_encryption_key)
        return self._cipher_instance

    async def _credentials(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(Integration).where(
                        Integration.tenant_id == tenant_id,
                        Integration.provider == "hubspot",
                    )
                )
            ).scalar_one_or_none()
            if row is None or row.status != "connected":
                raise PermanentIntegrationError(
                    f"tenant {tenant_id} has no connected HubSpot integration"
                )
            credentials: dict[str, Any] = json.loads(
                self._cipher.decrypt(row.credentials_encrypted)
            )
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
            raise PermanentIntegrationError("HubSpot token expired and no refresh token")

        async with self._http() as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._settings.hubspot_client_id,
                    "client_secret": self._settings.hubspot_client_secret,
                },
            )
        if response.status_code >= 400:
            # A revoked grant never recovers by retrying — mark it so the
            # dashboard can prompt a reconnect.
            await self._mark_status(tenant_id, integration_id, "revoked")
            raise PermanentIntegrationError(f"HubSpot token refresh failed: {response.status_code}")

        payload = response.json()
        credentials["access_token"] = payload["access_token"]
        if payload.get("refresh_token"):
            credentials["refresh_token"] = payload["refresh_token"]
        credentials["expiry"] = (
            datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 1800)))
        ).isoformat()

        async with tenant_session(tenant_id) as session:
            await session.execute(
                table_of(Integration)
                .update()
                .where(Integration.id == integration_id)
                .values(credentials_encrypted=self._cipher.encrypt(json.dumps(credentials)))
            )
        logger.info("hubspot_token_refreshed", tenant_id=str(tenant_id))
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
        # Generous relative to the calendar's 5s: this runs on the async path
        # where correctness matters more than latency.
        return httpx.AsyncClient(timeout=20)

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        if response.status_code < 400:
            return
        detail = response.text[:200]
        if response.status_code in PERMANENT_STATUSES:
            raise PermanentIntegrationError(
                f"HubSpot {action} rejected ({response.status_code}): {detail}"
            )
        raise IntegrationError(f"HubSpot {action} failed ({response.status_code}): {detail}")

    # ── contacts ───────────────────────────────────────────────────────────

    async def upsert_contact(self, tenant_id: uuid.UUID, contact: ContactRecord) -> str:
        credentials = await self._credentials(tenant_id)
        headers = {"Authorization": f"Bearer {credentials['access_token']}"}
        properties = _contact_properties(contact)

        existing_id = None
        if not contact.email:
            # No email means HubSpot cannot dedupe for us; match on phone
            # ourselves or we create a duplicate contact on every call.
            existing_id = await self._find_contact_by_phone(headers, contact.phone)

        if existing_id is None:
            async with self._http() as client:
                response = await client.post(
                    f"{API_BASE_URL}/crm/v3/objects/contacts",
                    headers=headers,
                    json={"properties": properties},
                )
            if response.status_code == 409:
                # Email already exists — HubSpot puts the existing id in the
                # error message, but parsing prose is brittle, so look it up.
                existing_id = await self._find_contact_by_email(headers, contact.email or "")
                if existing_id is None:
                    raise IntegrationError("HubSpot reported a duplicate contact it cannot find")
            else:
                self._raise_for_status(response, "contact create")
                return str(response.json()["id"])

        async with self._http() as client:
            response = await client.patch(
                f"{API_BASE_URL}/crm/v3/objects/contacts/{existing_id}",
                headers=headers,
                json={"properties": properties},
            )
        self._raise_for_status(response, "contact update")
        return str(existing_id)

    async def _find_contact_by_phone(self, headers: dict[str, str], phone: str) -> str | None:
        if not phone:
            return None
        return await self._search_contacts(headers, "phone", phone)

    async def _find_contact_by_email(self, headers: dict[str, str], email: str) -> str | None:
        if not email:
            return None
        return await self._search_contacts(headers, "email", email)

    async def _search_contacts(
        self, headers: dict[str, str], property_name: str, value: str
    ) -> str | None:
        async with self._http() as client:
            response = await client.post(
                f"{API_BASE_URL}/crm/v3/objects/contacts/search",
                headers=headers,
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": property_name,
                                    "operator": "EQ",
                                    "value": value,
                                }
                            ]
                        }
                    ],
                    "limit": 1,
                },
            )
        self._raise_for_status(response, "contact search")
        results = response.json().get("results", [])
        return str(results[0]["id"]) if results else None

    # ── activities ─────────────────────────────────────────────────────────

    async def log_activity(self, tenant_id: uuid.UUID, activity: ActivityRecord) -> str:
        credentials = await self._credentials(tenant_id)
        headers = {"Authorization": f"Bearer {credentials['access_token']}"}
        body = {
            "properties": {
                # HubSpot wants epoch milliseconds, not ISO 8601, on calls.
                "hs_timestamp": int(activity.occurred_at.timestamp() * 1000),
                "hs_call_title": "AI receptionist call",
                "hs_call_body": activity.summary,
                "hs_call_duration": activity.duration_seconds * 1000,
                "hs_call_direction": "INBOUND",
                "hs_call_status": "COMPLETED",
            },
            "associations": [
                {
                    "to": {"id": activity.contact_external_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": ASSOC_CALL_TO_CONTACT,
                        }
                    ],
                }
            ],
        }
        async with self._http() as client:
            response = await client.post(
                f"{API_BASE_URL}/crm/v3/objects/calls", headers=headers, json=body
            )
        self._raise_for_status(response, "activity create")
        return str(response.json()["id"])

    # ── deals ──────────────────────────────────────────────────────────────

    async def create_deal(self, tenant_id: uuid.UUID, deal: DealRecord) -> str:
        credentials = await self._credentials(tenant_id)
        headers = {"Authorization": f"Bearer {credentials['access_token']}"}
        properties: dict[str, Any] = {
            "dealname": deal.title,
            "dealstage": deal.stage,
            "pipeline": deal.properties.get("pipeline", "default"),
        }
        if deal.amount is not None:
            properties["amount"] = str(deal.amount)
        if deal.close_date is not None:
            properties["closedate"] = int(deal.close_date.timestamp() * 1000)

        body = {
            "properties": properties,
            "associations": [
                {
                    "to": {"id": deal.contact_external_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": ASSOC_DEAL_TO_CONTACT,
                        }
                    ],
                }
            ],
        }
        async with self._http() as client:
            response = await client.post(
                f"{API_BASE_URL}/crm/v3/objects/deals", headers=headers, json=body
            )
        self._raise_for_status(response, "deal create")
        return str(response.json()["id"])


def _contact_properties(contact: ContactRecord) -> dict[str, Any]:
    """Normalized record → HubSpot's property names.

    Name splitting is naive on purpose: HubSpot insists on first/last, callers
    give one spoken string, and a wrong split is visibly better than dropping
    the name. Anything past the first token becomes the last name so
    "Mary Anne Smith" keeps "Anne Smith" rather than losing it.
    """
    properties: dict[str, Any] = {"phone": contact.phone}
    if contact.email:
        properties["email"] = contact.email
    if contact.name:
        first, _, last = contact.name.strip().partition(" ")
        properties["firstname"] = first
        if last:
            properties["lastname"] = last
    properties.update(contact.properties)
    return properties
