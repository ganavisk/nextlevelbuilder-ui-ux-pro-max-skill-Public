"""Keep the voice vendor's agent in step with the tenant's configuration.

The vendor (Vapi) runs a copy of the agent: prompt, greeting, voice and tool
schemas, built by `service.build_agent_definition`. Until this module existed
that copy only changed when someone ran `make provision`, so a dashboard save
silently didn't reach callers.

Flow:

1. A dashboard save calls `resync_if_changed` with the agent definition from
   *before* the save. If the definition differs afterwards, the tenant's
   `voice_sync_requested` version is bumped and a `voice_sync` job is queued.
   Diffing the whole definition, instead of listing which settings matter,
   means a new prompt input can't be forgotten here.
2. The job (`workers/voice_sync.py`) reads the requested version, pushes the
   agent with `push_agent`, and records that version as live
   (`mark_synced`). On the last failed attempt it records the error
   (`mark_failed`).
3. `get_status` turns the versions into what the dashboard shows: live,
   updating, failed, or not_connected (never provisioned).

Provisioning a phone number stays a deliberate step (`make provision`); a
tenant with no number on the vendor has nothing to re-sync.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import case, func, select, update

from app.core.db import table_of, tenant_session
from app.core.errors import TenantNotFoundError
from app.core.logging import get_logger
from app.core.queue import VOICE_SYNC_QUEUE, enqueue
from app.modules.telephony.providers.base import AgentDefinition, VoiceProvider
from app.modules.tenants.models import PhoneNumber, Tenant
from app.modules.tenants.repository import get_tenant
from app.modules.tenants.service import build_agent_definition

logger = get_logger(__name__)

VoiceSyncState = Literal["live", "updating", "failed", "not_connected"]


@dataclass(frozen=True)
class VoiceSyncStatus:
    state: VoiceSyncState
    synced_at: datetime | None
    error: str


# ── reading state ───────────────────────────────────────────────────────


async def load_numbers(tenant_id: uuid.UUID) -> list[PhoneNumber]:
    async with tenant_session(tenant_id) as session:
        numbers = list(
            (
                await session.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id))
            ).scalars()
        )
        for number in numbers:
            session.expunge(number)
    return numbers


def is_connected(numbers: list[PhoneNumber]) -> bool:
    """Provisioned: some number already points at a vendor agent."""
    return any(n.vendor_agent_id for n in numbers)


async def get_status(tenant_id: uuid.UUID) -> VoiceSyncStatus:
    tenant = await get_tenant(tenant_id)
    if not is_connected(await load_numbers(tenant_id)):
        return VoiceSyncStatus("not_connected", tenant.voice_synced_at, "")
    if tenant.voice_sync_done >= tenant.voice_sync_requested:
        return VoiceSyncStatus("live", tenant.voice_synced_at, "")
    if tenant.voice_sync_error and tenant.voice_sync_failed_version >= tenant.voice_sync_requested:
        return VoiceSyncStatus("failed", tenant.voice_synced_at, tenant.voice_sync_error)
    return VoiceSyncStatus("updating", tenant.voice_synced_at, "")


async def agent_snapshot(tenant_id: uuid.UUID) -> AgentDefinition | None:
    """What the vendor agent should currently be, or None if the tenant has
    no agent config yet (nothing to sync)."""
    tenant = await get_tenant(tenant_id)
    try:
        return await build_agent_definition(tenant, tenant_id)
    except TenantNotFoundError:
        return None


# ── requesting a sync ───────────────────────────────────────────────────


async def resync_if_changed(
    tenant_id: uuid.UUID, before: AgentDefinition | None, *, redis: Redis
) -> bool:
    """Queue a sync if the save changed the agent. Returns whether it did."""
    after = await agent_snapshot(tenant_id)
    if after is None or after == before:
        return False
    await request_sync(tenant_id, redis=redis)
    return True


async def request_sync(tenant_id: uuid.UUID, *, redis: Redis) -> int:
    """Bump the requested version and queue a push. Returns the new version.

    The bump comes before the enqueue, so whichever job runs reads a version
    at least this new. If the tenant isn't connected to the vendor yet, only
    the version moves: `make provision` will push everything and record it.
    """
    async with tenant_session(tenant_id) as session:
        version = (
            await session.execute(
                update(table_of(Tenant))
                .where(Tenant.id == tenant_id)
                .values(voice_sync_requested=Tenant.voice_sync_requested + 1)
                .returning(Tenant.voice_sync_requested)
            )
        ).scalar_one()
    if not is_connected(await load_numbers(tenant_id)):
        return int(version)
    try:
        await enqueue(redis, VOICE_SYNC_QUEUE, {"type": "voice_sync", "tenant_id": str(tenant_id)})
    except Exception as exc:
        # The save itself succeeded; say plainly that it isn't live, rather
        # than leaving the dashboard on "updating" for a job that never runs.
        logger.exception("voice_sync_enqueue_failed", tenant_id=str(tenant_id))
        await mark_failed(
            tenant_id, int(version), f"Couldn't queue the update ({type(exc).__name__})."
        )
    return int(version)


# ── recording outcomes ──────────────────────────────────────────────────


async def mark_synced(tenant_id: uuid.UUID, version: int) -> None:
    """`version` is live. Never moves `done` backwards (jobs can finish out
    of order) and keeps a newer failure's error visible."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            update(table_of(Tenant))
            .where(Tenant.id == tenant_id)
            .values(
                voice_sync_done=func.greatest(Tenant.voice_sync_done, version),
                voice_synced_at=func.now(),
                voice_sync_error=case(
                    (Tenant.voice_sync_failed_version > version, Tenant.voice_sync_error),
                    else_="",
                ),
            )
        )


async def mark_failed(tenant_id: uuid.UUID, version: int, error: str) -> None:
    """`version` couldn't be pushed. Ignored if a later push already covered
    it, so a slow failing job can't overwrite a newer success."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            update(table_of(Tenant))
            .where(Tenant.id == tenant_id, Tenant.voice_sync_done < version)
            .values(
                voice_sync_failed_version=func.greatest(Tenant.voice_sync_failed_version, version),
                voice_sync_error=error[:300],
            )
        )


# ── pushing to the vendor ───────────────────────────────────────────────


async def push_agent(
    tenant: Tenant,
    numbers: list[PhoneNumber],
    provider: VoiceProvider,
    *,
    vendor: str,
    attach_all: bool = False,
) -> str:
    """Create or update the tenant's vendor agent and make sure its numbers
    point at it. Returns the vendor agent id.

    One vendor agent per tenant; every number points at it. If the stored
    agent was deleted on the vendor side, `sync_agent` creates a new one,
    and the numbers are re-attached here so calls don't keep ringing the
    deleted agent. `attach_all` re-attaches every number even when nothing
    moved (`make provision` does, to repair vendor-side drift).
    """
    definition = await build_agent_definition(tenant, tenant.id)
    existing = next((n.vendor_agent_id for n in numbers if n.vendor_agent_id), "")
    agent_id = await provider.sync_agent(definition, existing_agent_id=existing)
    logger.info(
        "voice_agent_synced",
        tenant_id=str(tenant.id),
        vendor_agent_id=agent_id,
        updated=bool(existing),
        replaced=bool(existing) and agent_id != existing,
    )
    to_attach = numbers if attach_all else [n for n in numbers if n.vendor_agent_id != agent_id]
    if not to_attach:
        return agent_id
    async with tenant_session(tenant.id) as session:
        for number in to_attach:
            vendor_number_id = await provider.attach_number(agent_id, number.e164)
            await session.execute(
                update(table_of(PhoneNumber))
                .where(PhoneNumber.id == number.id)
                .values(vendor_agent_id=agent_id, vendor_number_id=vendor_number_id, vendor=vendor)
            )
            logger.info("number_attached", e164=number.e164, vendor_number_id=vendor_number_id)
    return agent_id
