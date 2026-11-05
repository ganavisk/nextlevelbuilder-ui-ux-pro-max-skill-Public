"""CRM sync service, against real Postgres.

The property that matters is resumability: a sync that fails partway through
must, on retry, complete only the steps that didn't already succeed. HubSpot
has no idempotency key on deal creation, so replaying a completed step is how
a customer's CRM ends up with two deals for one phone call.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.errors import IntegrationError
from app.modules.crm.service import failed_syncs, mark_dead, sync_call
from app.modules.tenants.models import Tenant
from tests.conftest import requires_services
from tests.fakes import FakeCRMProvider

CRM_SETTINGS = {
    "crm": {
        "stage_by_outcome": {"appointment_booked": "appointmentscheduled", "default": "new"},
        "contact_properties": {"pain": "urgency"},
        "deal_min_score": 30,
    }
}


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


def _tenant(tenant_id: uuid.UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        slug=f"crm-{tenant_id.hex[:8]}",
        name="Bright Smile Dental",
        vertical="dental",
        timezone="America/New_York",
        business_hours={},
        settings=CRM_SETTINGS,
        status="active",
    )


@pytest.fixture
def tenant_with_lead():
    """A tenant with one call and one lead attached to it. Yields
    (Tenant, call_id)."""
    tenant_id, call_id, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, settings) "
                "VALUES (:id, :slug, 'CRM Test', 'dental', CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": f"crm-{tenant_id.hex[:8]}",
                "settings": json.dumps(CRM_SETTINGS),
            },
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO calls (id, tenant_id, vendor_call_id, caller_e164) "
                "VALUES (:id, :tid, 'vendor-call-1', '+15550001111')"
            ),
            {"id": str(call_id), "tid": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO leads (id, tenant_id, call_id, name, phone, email, score, "
                "qualification) VALUES (:id, :tid, :cid, 'Dana Lee', '+15550001111', "
                "'dana@example.com', 50, CAST(:qual AS jsonb))"
            ),
            {
                "id": str(lead_id),
                "tid": str(tenant_id),
                "cid": str(call_id),
                "qual": json.dumps({"pain": True}),
            },
        )
    try:
        yield _tenant(tenant_id), call_id
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


def _tenant_session(tenant_id):
    from app.core.db import tenant_session

    return tenant_session(tenant_id)


# ── happy path ───────────────────────────────────────────────────────────


@requires_services
async def test_full_sync_creates_contact_activity_and_deal(tenant_with_lead) -> None:
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()

    outcome = await sync_call(
        tenant=tenant,
        call_id=call_id,
        crm=crm,
        outcome="appointment_booked",
        summary="Caller booked a cleaning.",
        occurred_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        duration_seconds=90,
        service="Cleaning",
    )

    assert len(crm.contacts) == 1
    assert len(crm.activities) == 1
    assert len(crm.deals) == 1
    assert outcome.contact_external_id == "contact-1"
    assert outcome.deal_external_id == "deal-1"
    assert crm.contacts[0].properties == {"urgency": "true"}


@requires_services
async def test_low_scoring_faq_call_gets_no_deal(tenant_with_lead) -> None:
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()

    outcome = await sync_call(
        tenant=tenant, call_id=call_id, crm=crm, outcome="answered_faq", summary="Asked hours."
    )

    assert len(crm.contacts) == 1
    assert len(crm.activities) == 1
    assert crm.deals == []
    assert outcome.deal_external_id is None


@requires_services
async def test_call_with_no_lead_is_skipped_not_failed(tenant_with_lead) -> None:
    """A wrong number or hang-up has no lead. Skipping must not raise — a
    real failure would retry forever for a call that will never have one."""
    tenant, _ = tenant_with_lead
    crm = FakeCRMProvider()
    outcome = await sync_call(
        tenant=tenant, call_id=uuid.uuid4(), crm=crm, outcome=None, summary=""
    )
    assert outcome.skipped is True
    assert outcome.reason == "no_lead"
    assert crm.contacts == []


# ── resumability: the headline guarantee ────────────────────────────────


@requires_services
async def test_resumed_sync_does_not_repeat_the_contact_step(tenant_with_lead) -> None:
    """If the deal step fails, retrying must not upsert the contact again —
    upsert is safe to repeat, but proving it isn't repeated is what proves
    the resume logic actually checks state rather than just being lucky."""
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    crm.fail_deal = IntegrationError("HubSpot 503")

    with pytest.raises(IntegrationError):
        await sync_call(
            tenant=tenant, call_id=call_id, crm=crm, outcome="appointment_booked", summary="s"
        )

    assert len(crm.contacts) == 1
    assert len(crm.activities) == 1
    assert crm.deals == []

    outcome = await sync_call(
        tenant=tenant, call_id=call_id, crm=crm, outcome="appointment_booked", summary="s"
    )

    # The resume must not have called upsert_contact or log_activity again.
    assert len(crm.contacts) == 1
    assert len(crm.activities) == 1
    assert len(crm.deals) == 1
    assert outcome.deal_external_id == "deal-1"


@requires_services
async def test_resumed_sync_does_not_repeat_the_activity_step(tenant_with_lead) -> None:
    """The step with no natural key at all: a repeated log_activity call
    would show the same phone call twice on the contact's timeline."""
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    crm.fail_activity = IntegrationError("HubSpot 503")

    with pytest.raises(IntegrationError):
        await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")

    assert len(crm.contacts) == 1
    assert crm.activities == []

    await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")

    assert len(crm.contacts) == 1, "the contact step must not repeat on resume"
    assert len(crm.activities) == 1


@requires_services
async def test_synced_call_is_a_pure_replay_no_writes_at_all(tenant_with_lead) -> None:
    """Once fully synced, a redelivered job (queue retries are
    at-least-once) must touch the vendor zero times."""
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    await sync_call(
        tenant=tenant, call_id=call_id, crm=crm, outcome="appointment_booked", summary="s"
    )
    counts_after_first = (len(crm.contacts), len(crm.activities), len(crm.deals))

    outcome = await sync_call(
        tenant=tenant, call_id=call_id, crm=crm, outcome="appointment_booked", summary="s"
    )

    assert (len(crm.contacts), len(crm.activities), len(crm.deals)) == counts_after_first
    assert outcome.skipped is True
    assert outcome.reason == "already_synced"


@requires_services
async def test_two_workers_racing_the_same_call_do_not_double_write(
    tenant_with_lead,
) -> None:
    """The zombie-worker scenario core/queue.py's docstring warns about: a
    reclaimed job can run concurrently with its still-alive original. Exactly
    one attempt must do the work; the other must claim nothing and write
    nothing, not merely avoid duplicating the final result."""
    import asyncio

    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()

    results = await asyncio.gather(
        sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s"),
        sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s"),
    )

    assert len({r.sync_id for r in results}) == 1
    assert len(crm.contacts) == 1
    assert len(crm.activities) == 1
    # Exactly one side actually ran; the loser reports it explicitly rather
    # than silently doing nothing and looking identical to a success.
    reasons = sorted(r.reason for r in results)
    assert reasons == ["", "in_progress_elsewhere"]


# ── error paths ──────────────────────────────────────────────────────────


@requires_services
async def test_partial_failure_is_visible_in_failed_syncs(tenant_with_lead) -> None:
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    crm.fail_contact = IntegrationError("HubSpot down")

    with pytest.raises(IntegrationError):
        await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")

    failed = await failed_syncs(tenant.id)
    assert len(failed) == 1
    assert failed[0].status == "failed"
    assert failed[0].contact_external_id is None
    assert "HubSpot down" in (failed[0].last_error or "")


@requires_services
async def test_mark_dead_is_visible_in_the_database_not_only_redis(tenant_with_lead) -> None:
    """The queue's dead-letter list lives in Redis, invisible to the
    dashboard. mark_dead is what makes a terminal failure show up where a
    human — or Phase 7's UI — can actually see it."""
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    crm.fail_contact = IntegrationError("permanently broken")

    with pytest.raises(IntegrationError):
        await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")
    await mark_dead(tenant.id, call_id, "max attempts exceeded")

    failed = await failed_syncs(tenant.id)
    assert len(failed) == 1
    assert failed[0].status == "dead"
    assert "max attempts" in (failed[0].last_error or "")


@requires_services
async def test_attempt_count_increments_across_retries(tenant_with_lead) -> None:
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    crm.fail_contact = IntegrationError("blip")

    with pytest.raises(IntegrationError):
        await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")
    await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")

    async with _tenant_session(tenant.id) as session:
        from sqlalchemy import select

        from app.modules.crm.models import CrmSync

        row = (
            await session.execute(select(CrmSync).where(CrmSync.call_id == call_id))
        ).scalar_one()
        assert row.attempts == 2
        assert row.status == "synced"


@requires_services
async def test_lead_is_stamped_with_crm_contact_id_and_synced_at(tenant_with_lead) -> None:
    """The dashboard links out to the CRM from the lead row (Phase 1's data
    model put `crm_contact_id` there for exactly this)."""
    tenant, call_id = tenant_with_lead
    crm = FakeCRMProvider()
    await sync_call(tenant=tenant, call_id=call_id, crm=crm, outcome=None, summary="s")

    async with _tenant_session(tenant.id) as session:
        from sqlalchemy import select

        from app.modules.conversation.models import Lead

        lead = (await session.execute(select(Lead).where(Lead.call_id == call_id))).scalar_one()
        assert lead.crm_contact_id == "contact-1"
        assert lead.crm_synced_at is not None
