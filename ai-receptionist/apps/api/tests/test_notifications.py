"""Confirmation dispatch, against real Postgres.

The property that matters is that a customer is never messaged twice. Queue
redelivery is normal, not exceptional, so the claim-then-send ordering is
tested directly — including two workers racing the same job.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.db import tenant_session
from app.modules.notifications.service import pending_for_appointment, send_email, send_sms
from app.modules.notifications.templates import (
    appointment_confirmation_email,
    appointment_confirmation_sms,
)
from app.workers.confirmations import Deps, handle_confirmation
from tests.conftest import requires_services
from tests.fakes import FakeEmailProvider, FakeSMSProvider

APPOINTMENT_START = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def _sync_engine():
    settings = Settings(_env_file=None, app_env="test")
    return create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )


@pytest.fixture
def seeded():
    """A tenant with one confirmed appointment. Returns (tenant_id, appointment_id)."""
    tenant_id, appointment_id = uuid.uuid4(), uuid.uuid4()
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, settings) "
                "VALUES (:id, :slug, 'Notify Test', 'dental', 'America/New_York', "
                "CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": f"notify-{tenant_id.hex[:8]}",
                "settings": json.dumps(
                    {"notifications": {"email_confirmation": True, "sms_confirmation": True}}
                ),
            },
        )
        # appointments is RLS-protected: the insert needs tenant context, the
        # same way the application sets it.
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text(
                "INSERT INTO appointments (id, tenant_id, service, starts_at, ends_at, "
                "status, customer_name, customer_phone, customer_email, timezone) "
                "VALUES (:id, :tid, 'Cleaning', :s, :e, 'confirmed', 'Dana', "
                "'+15550001111', 'dana@example.com', 'America/New_York')"
            ),
            {
                "id": str(appointment_id),
                "tid": str(tenant_id),
                "s": APPOINTMENT_START,
                "e": APPOINTMENT_START + timedelta(hours=1),
            },
        )
    try:
        yield tenant_id, appointment_id
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()


@requires_services
async def test_repeated_job_delivery_sends_only_once(seeded) -> None:
    """The core idempotency guarantee: a redelivered queue job must not send
    the customer a second confirmation."""
    tenant_id, appointment_id = seeded
    deps = Deps(
        settings=Settings(_env_file=None, app_env="test"),
        email=FakeEmailProvider(),
        sms=FakeSMSProvider(),
    )
    job = {"tenant_id": str(tenant_id), "appointment_id": str(appointment_id)}

    await handle_confirmation(job, deps=deps)
    await handle_confirmation(job, deps=deps)
    await handle_confirmation(job, deps=deps)

    assert len(deps.email.sent) == 1
    assert len(deps.sms.sent) == 1
    assert deps.email.sent[0]["to"] == "dana@example.com"
    assert deps.sms.sent[0]["to"] == "+15550001111"


@requires_services
async def test_two_workers_racing_one_job_send_once(seeded) -> None:
    """Claim-then-send has to hold under genuine concurrency, not just
    sequential redelivery — two workers can pick up the same job."""
    tenant_id, appointment_id = seeded
    email, sms = FakeEmailProvider(), FakeSMSProvider()
    settings = Settings(_env_file=None, app_env="test")
    job = {"tenant_id": str(tenant_id), "appointment_id": str(appointment_id)}

    await asyncio.gather(
        handle_confirmation(job, deps=Deps(settings=settings, email=email, sms=sms)),
        handle_confirmation(job, deps=Deps(settings=settings, email=email, sms=sms)),
    )

    assert len(email.sent) == 1
    assert len(sms.sent) == 1


@requires_services
async def test_failing_sms_does_not_cost_the_customer_their_email(seeded) -> None:
    """Channels are dispatched independently; one vendor outage must not
    suppress the other channel."""
    tenant_id, appointment_id = seeded
    sms = FakeSMSProvider()
    sms.fail = RuntimeError("twilio is down")
    deps = Deps(
        settings=Settings(_env_file=None, app_env="test"), email=FakeEmailProvider(), sms=sms
    )

    with pytest.raises(RuntimeError):
        await handle_confirmation(
            {"tenant_id": str(tenant_id), "appointment_id": str(appointment_id)}, deps=deps
        )

    assert len(deps.email.sent) == 1
    rows = await pending_for_appointment(tenant_id, appointment_id)
    statuses = {r.channel: r.status for r in rows}
    assert statuses["email"] == "sent"
    assert statuses["sms"] == "failed"


@requires_services
async def test_cancelled_appointment_is_not_confirmed(seeded) -> None:
    """A job queued before a cancellation must not tell the customer they're
    booked."""
    tenant_id, appointment_id = seeded
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE appointments SET status = 'cancelled' WHERE id = :id"),
            {"id": str(appointment_id)},
        )

    deps = Deps(
        settings=Settings(_env_file=None, app_env="test"),
        email=FakeEmailProvider(),
        sms=FakeSMSProvider(),
    )
    await handle_confirmation(
        {"tenant_id": str(tenant_id), "appointment_id": str(appointment_id)}, deps=deps
    )

    assert deps.email.sent == []
    assert deps.sms.sent == []


@requires_services
async def test_tenant_can_disable_a_channel(seeded) -> None:
    tenant_id, appointment_id = seeded
    engine = _sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {
                "id": str(tenant_id),
                "s": json.dumps(
                    {"notifications": {"email_confirmation": True, "sms_confirmation": False}}
                ),
            },
        )
    engine.dispose()

    deps = Deps(
        settings=Settings(_env_file=None, app_env="test"),
        email=FakeEmailProvider(),
        sms=FakeSMSProvider(),
    )
    await handle_confirmation(
        {"tenant_id": str(tenant_id), "appointment_id": str(appointment_id)}, deps=deps
    )

    assert len(deps.email.sent) == 1
    assert deps.sms.sent == []


@requires_services
async def test_no_recipient_is_not_an_error(seeded) -> None:
    """Most callers give a phone number but no email. That's normal, not a
    failure — and it must not create a notification row."""
    tenant_id, appointment_id = seeded
    result = await send_email(
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        call_id=None,
        to="",
        subject="s",
        html="<p>h</p>",
        from_address="from@example.com",
        template="appointment_confirmation",
        idempotency_key=f"norecipient-{appointment_id}",
        provider=FakeEmailProvider(),
    )
    assert result.sent is False
    assert result.reason == "no_recipient"
    assert await pending_for_appointment(tenant_id, appointment_id) == []


@requires_services
async def test_second_claim_reports_already_claimed(seeded) -> None:
    tenant_id, appointment_id = seeded
    provider = FakeSMSProvider()
    common = dict(
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        call_id=None,
        to="+15550001111",
        body="See you Tuesday.",
        template="appointment_confirmation",
        idempotency_key=f"claim-{appointment_id}",
        provider=provider,
    )
    first = await send_sms(**common)
    second = await send_sms(**common)

    assert first.sent is True
    assert second.sent is False
    assert second.reason == "already_claimed"
    assert len(provider.sent) == 1


def test_sms_template_carries_opt_out() -> None:
    """Transactional SMS still needs an opt-out path (TCPA)."""
    body = appointment_confirmation_sms(
        business_name="Bright Smile Dental",
        service="Cleaning",
        starts_at=APPOINTMENT_START,
        timezone="America/New_York",
    )
    assert "STOP" in body
    assert "Bright Smile Dental" in body


def test_email_template_states_the_local_time() -> None:
    """14:00 UTC is 10am in New York — the customer must see their own time,
    not ours."""
    rendered = appointment_confirmation_email(
        business_name="Bright Smile Dental",
        customer_name="Dana",
        service="Cleaning",
        starts_at=APPOINTMENT_START,
        timezone="America/New_York",
    )
    assert "Tuesday, September 15 at 10 am" in rendered.html
    assert "Dana" in rendered.html
    assert "Bright Smile Dental" in rendered.subject
