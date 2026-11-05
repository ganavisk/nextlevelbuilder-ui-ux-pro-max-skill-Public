"""CRMProvider — CRM backends (HubSpot first; Pipedrive/Zoho as follow-ons).

CRM writes happen on the async post-call path only (never while the caller
waits) and are retried with backoff. Field mapping per tenant lives in
`crm.mapping`; this interface moves normalized contacts, activities and deals.

Every method returns the external id it created or found. That is not
decoration: the CRM APIs we target have no idempotency key, so the id we get
back is the only thing that lets a retry skip a step it already completed.
`crm.service` records each one before attempting the next.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ContactRecord:
    phone: str
    name: str | None = None
    email: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivityRecord:
    """A call, as it should appear on the contact's timeline."""

    contact_external_id: str
    summary: str
    occurred_at: datetime
    duration_seconds: int = 0
    outcome: str = ""


@dataclass(frozen=True)
class DealRecord:
    contact_external_id: str
    title: str
    stage: str
    amount: float | None = None
    close_date: datetime | None = None
    properties: dict[str, Any] = field(default_factory=dict)


class CRMProvider(ABC):
    @abstractmethod
    async def upsert_contact(self, tenant_id: uuid.UUID, contact: ContactRecord) -> str:
        """Create or update by email/phone; return the external contact id.

        Must be safe to call twice with the same record: an existing contact
        is updated, never duplicated. This is the one method a retry may
        legitimately repeat, so it carries the burden of matching.
        """

    @abstractmethod
    async def log_activity(self, tenant_id: uuid.UUID, activity: ActivityRecord) -> str:
        """Attach a call to the contact's timeline; return the activity id."""

    @abstractmethod
    async def create_deal(self, tenant_id: uuid.UUID, deal: DealRecord) -> str:
        """Create a deal and associate it with the contact; return its id.

        Deliberately *create*, not upsert: there is no natural key for a deal,
        so the caller must not call this twice. `crm.service` records the
        returned id and skips this step on any later attempt.
        """
