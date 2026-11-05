"""CRM synchronization: resumable, per-step, never duplicating.

## Why this is not just "call three endpoints"

A sync is three writes against an API with no idempotency key: upsert the
contact, log the call, create the deal. Any of them can fail. If the job is
simply retried from the top, the steps that already succeeded run again — and
`create_deal` has no natural key, so the practice ends up with two deals for
one phone call. Duplicates in a customer's CRM are worse than a missing
record: they get noticed, and a human has to merge them.

So each external id is written to `crm_syncs` the moment it is obtained, and
every attempt skips the steps that already have one:

    attempt 1:  contact ✓ (id saved)  activity ✓ (id saved)  deal ✗ ── raise
    attempt 2:  contact skipped        activity skipped        deal ✓

The ledger row is found via a unique idempotency key, so two workers handling
the same job converge on one row rather than two. Converging on one row is
not by itself enough: the queue's delivery guarantee is at-least-once (see
core/queue.py), so a worker presumed dead can be reclaimed while genuinely
still running, and the reclaimed job then executes concurrently with its
"zombie" original. Both would see the same NULL columns and both write.

`_claim_attempt` closes that gap with an atomic status transition — only one
concurrent caller can move the row from `pending`/`failed` (or a stale
`in_progress`) to `in_progress`; the loser does no work at all rather than
racing step-by-step.

Nothing here runs on the call path.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import table_of, tenant_session
from app.core.logging import get_logger
from app.modules.conversation.models import Lead
from app.modules.crm import mapping
from app.modules.crm.models import CrmSync
from app.modules.crm.providers.base import ActivityRecord, CRMProvider
from app.modules.tenants.models import Tenant

logger = get_logger(__name__)

# An `in_progress` claim older than this is presumed abandoned (the worker
# that held it crashed) rather than merely slow, and may be reclaimed. Well
# above the CRM HTTP timeout (20s) times three steps, so a legitimately
# in-flight attempt is never preempted.
CLAIM_STALE_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class SyncOutcome:
    sync_id: uuid.UUID
    contact_external_id: str | None
    activity_external_id: str | None
    deal_external_id: str | None
    skipped: bool = False
    reason: str = ""


def sync_key(call_id: uuid.UUID) -> str:
    """One sync per call. Re-queuing the same call resumes its ledger row
    rather than starting a second sync."""
    return f"call:{call_id}"


async def _claim_or_load(
    *, tenant_id: uuid.UUID, call_id: uuid.UUID, lead_id: uuid.UUID | None, key: str
) -> uuid.UUID:
    """Get the ledger row for this call, creating it if this is the first
    attempt. ON CONFLICT DO NOTHING plus a follow-up read means two concurrent
    workers end up on the same row instead of two."""
    async with tenant_session(tenant_id) as session:
        stmt = (
            pg_insert(CrmSync)
            .values(
                tenant_id=tenant_id,
                call_id=call_id,
                lead_id=lead_id,
                idempotency_key=key,
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(CrmSync.id)
        )
        sync_id = (await session.execute(stmt)).scalar_one_or_none()
        if sync_id is None:
            sync_id = (
                await session.execute(select(CrmSync.id).where(CrmSync.idempotency_key == key))
            ).scalar_one()
        return sync_id


async def _claim_attempt(tenant_id: uuid.UUID, sync_id: uuid.UUID) -> bool:
    """Atomically take ownership of this sync row for one attempt.

    Succeeds against `pending`, `failed`, or a stale `in_progress` (see
    CLAIM_STALE_AFTER). A concurrent caller that loses this UPDATE — because
    someone else's `in_progress`/`claimed_at` is still fresh — gets zero rows
    back and must not perform any step; that is what makes step-level races
    impossible rather than merely unlikely.
    """
    now = datetime.now(UTC)
    stale_before = now - CLAIM_STALE_AFTER
    async with tenant_session(tenant_id) as session:
        result = await session.execute(
            table_of(CrmSync)
            .update()
            .where(
                CrmSync.id == sync_id,
                or_(
                    CrmSync.status.in_(("pending", "failed")),
                    (CrmSync.status == "in_progress") & (CrmSync.claimed_at < stale_before),
                ),
            )
            .values(status="in_progress", claimed_at=now, attempts=CrmSync.attempts + 1)
            .returning(CrmSync.id)
        )
        return result.scalar_one_or_none() is not None


async def _load(tenant_id: uuid.UUID, sync_id: uuid.UUID) -> CrmSync:
    async with tenant_session(tenant_id) as session:
        row = (await session.execute(select(CrmSync).where(CrmSync.id == sync_id))).scalar_one()
        session.expunge(row)
        return row


async def _record(tenant_id: uuid.UUID, sync_id: uuid.UUID, **values: Any) -> None:
    """Persist progress immediately. Called after *each* external write, so a
    crash between steps still leaves the completed ones marked done."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(CrmSync).update().where(CrmSync.id == sync_id).values(**values)
        )


async def sync_call(
    *,
    tenant: Tenant,
    call_id: uuid.UUID,
    crm: CRMProvider,
    outcome: str | None,
    summary: str,
    occurred_at: datetime | None = None,
    duration_seconds: int = 0,
    service: str = "",
) -> SyncOutcome:
    """Synchronize one call's lead to the tenant's CRM.

    Raises on integration failure so the queue can retry; the ledger holds
    whatever progress was made.
    """
    occurred_at = occurred_at or datetime.now(UTC)

    async with tenant_session(tenant.id) as session:
        lead = (
            await session.execute(select(Lead).where(Lead.call_id == call_id))
        ).scalar_one_or_none()
        if lead is not None:
            session.expunge(lead)

    if lead is None:
        # No lead means nobody identifiable to sync — a wrong number or a
        # hang-up. Not a failure, and not worth a ledger row.
        logger.info("crm_sync_skipped", call_id=str(call_id), reason="no_lead")
        return SyncOutcome(
            sync_id=uuid.uuid4(),
            contact_external_id=None,
            activity_external_id=None,
            deal_external_id=None,
            skipped=True,
            reason="no_lead",
        )

    key = sync_key(call_id)
    sync_id = await _claim_or_load(tenant_id=tenant.id, call_id=call_id, lead_id=lead.id, key=key)
    state = await _load(tenant.id, sync_id)

    if state.status == "synced":
        logger.info("crm_sync_replayed", sync_id=str(sync_id))
        return SyncOutcome(
            sync_id=sync_id,
            contact_external_id=state.contact_external_id,
            activity_external_id=state.activity_external_id,
            deal_external_id=state.deal_external_id,
            skipped=True,
            reason="already_synced",
        )

    if not await _claim_attempt(tenant.id, sync_id):
        # Someone else's attempt is genuinely in flight (or a redelivery lost
        # the race to a peer that hasn't crashed). Doing no work here — not
        # even re-reading state to "help" — is what keeps this race-free;
        # the winner will finish or eventually fail, and either outcome is
        # visible on the next redelivery.
        logger.info("crm_sync_in_progress_elsewhere", sync_id=str(sync_id))
        return SyncOutcome(
            sync_id=sync_id,
            contact_external_id=None,
            activity_external_id=None,
            deal_external_id=None,
            skipped=True,
            reason="in_progress_elsewhere",
        )
    state = await _load(tenant.id, sync_id)

    try:
        # ── 1. Contact. Safe to repeat, but skipped once known. ──
        contact_id = state.contact_external_id
        if contact_id is None:
            contact_id = await crm.upsert_contact(tenant.id, mapping.build_contact(tenant, lead))
            await _record(tenant.id, sync_id, contact_external_id=contact_id)
            # Mirror onto the lead: the dashboard links to the CRM from there,
            # and Phase 1 put the column on `leads` for exactly this.
            await _record_lead_contact(tenant.id, lead.id, contact_id)

        # ── 2. Activity. Not repeatable — a second call appears twice. ──
        activity_id = state.activity_external_id
        if activity_id is None:
            activity_id = await crm.log_activity(
                tenant.id,
                ActivityRecord(
                    contact_external_id=contact_id,
                    summary=summary,
                    occurred_at=occurred_at,
                    duration_seconds=duration_seconds,
                    outcome=outcome or "",
                ),
            )
            await _record(tenant.id, sync_id, activity_external_id=activity_id)

        # ── 3. Deal, only when the call earned one. ──
        deal_id = state.deal_external_id
        if deal_id is None and mapping.should_create_deal(tenant, lead, outcome):
            deal_id = await crm.create_deal(
                tenant.id,
                mapping.build_deal(
                    tenant,
                    contact_external_id=contact_id,
                    lead=lead,
                    outcome=outcome,
                    service=service,
                ),
            )
            await _record(tenant.id, sync_id, deal_external_id=deal_id)
    except Exception as exc:
        await _record(
            tenant.id,
            sync_id,
            status="failed",
            last_error=f"{type(exc).__name__}: {exc}"[:500],
        )
        logger.warning(
            "crm_sync_failed",
            sync_id=str(sync_id),
            call_id=str(call_id),
            completed=state.pending_steps(),
        )
        raise

    await _record(tenant.id, sync_id, status="synced", synced_at=datetime.now(UTC), last_error=None)
    await _mark_lead_synced(tenant.id, lead.id)
    logger.info(
        "crm_synced",
        sync_id=str(sync_id),
        call_id=str(call_id),
        contact=contact_id,
        deal=deal_id,
    )
    return SyncOutcome(
        sync_id=sync_id,
        contact_external_id=contact_id,
        activity_external_id=activity_id,
        deal_external_id=deal_id,
    )


async def _record_lead_contact(tenant_id: uuid.UUID, lead_id: uuid.UUID, contact_id: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(Lead).update().where(Lead.id == lead_id).values(crm_contact_id=contact_id)
        )


async def _mark_lead_synced(tenant_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(Lead)
            .update()
            .where(Lead.id == lead_id)
            .values(crm_synced_at=datetime.now(UTC))
        )


async def mark_dead(tenant_id: uuid.UUID, call_id: uuid.UUID, reason: str) -> None:
    """Called when the queue gives up, so the failure is visible in the
    database rather than only in a Redis dead-letter list the dashboard
    cannot see."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(CrmSync)
            .update()
            .where(CrmSync.idempotency_key == sync_key(call_id))
            .values(status="dead", last_error=reason[:500])
        )


async def failed_syncs(tenant_id: uuid.UUID, *, limit: int = 50) -> list[CrmSync]:
    """The dashboard's 'needs attention' list (Phase 7 consumes this)."""
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(CrmSync)
                .where(CrmSync.status.in_(("failed", "dead")))
                .order_by(CrmSync.created_at.desc())
                .limit(limit)
            )
        ).scalars()
        return list(rows)
