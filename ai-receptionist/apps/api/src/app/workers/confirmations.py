"""Confirmation worker: sends the email and SMS for a booked appointment.

Runs off `queue:confirmations`, never on the call path — the caller hears
"you're booked" the moment the slot is reserved, and the messages go out
behind them.

Email and SMS are dispatched independently: a failing SMS provider must not
cost the customer their email confirmation.

Run: python -m app.workers.confirmations
"""

import asyncio
import functools
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

import app.models  # noqa: F401 — registers every ORM model on Base.metadata
from app.core.config import Settings, get_settings
from app.core.db import admin_session, ensure_rls_enforced, tenant_session
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry
from app.core.queue import (
    CONFIRMATIONS_QUEUE,
    reclaim_orphans,
    run_worker,
    stop_on_signal,
    worker_redis,
)
from app.modules.notifications.providers import get_email_provider, get_sms_provider
from app.modules.notifications.providers.base import EmailProvider, SMSProvider
from app.modules.notifications.service import send_email, send_sms
from app.modules.notifications.templates import (
    appointment_confirmation_email,
    appointment_confirmation_sms,
)
from app.modules.scheduling.models import Appointment
from app.modules.tenants.models import Tenant

logger = get_logger(__name__)


@dataclass
class Deps:
    settings: Settings
    email: EmailProvider
    sms: SMSProvider


async def handle_confirmation(job: dict[str, Any], *, deps: Deps) -> None:
    tenant_id = uuid.UUID(job["tenant_id"])
    appointment_id = uuid.UUID(job["appointment_id"])

    async with admin_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            logger.warning("confirmation_tenant_missing", tenant_id=str(tenant_id))
            return
        session.expunge(tenant)

    async with tenant_session(tenant_id) as session:
        appointment = (
            await session.execute(select(Appointment).where(Appointment.id == appointment_id))
        ).scalar_one_or_none()
        if appointment is None or appointment.status != "confirmed":
            logger.info("confirmation_skipped", appointment_id=str(appointment_id))
            return
        session.expunge(appointment)

    settings = tenant.settings or {}
    notify = settings.get("notifications", {})

    if notify.get("email_confirmation", True) and appointment.customer_email:
        rendered = appointment_confirmation_email(
            business_name=tenant.name,
            customer_name=appointment.customer_name,
            service=appointment.service,
            starts_at=appointment.starts_at,
            timezone=appointment.timezone,
        )
        await send_email(
            tenant_id=tenant_id,
            appointment_id=appointment_id,
            call_id=appointment.call_id,
            to=appointment.customer_email,
            subject=rendered.subject,
            html=rendered.html,
            from_address=deps.settings.email_from,
            template="appointment_confirmation",
            # Scoped to the appointment, so a redelivered job is a no-op.
            idempotency_key=f"appt:{appointment_id}:email",
            provider=deps.email,
        )

    if notify.get("sms_confirmation", True) and appointment.customer_phone:
        await send_sms(
            tenant_id=tenant_id,
            appointment_id=appointment_id,
            call_id=appointment.call_id,
            to=appointment.customer_phone,
            body=appointment_confirmation_sms(
                business_name=tenant.name,
                service=appointment.service,
                starts_at=appointment.starts_at,
                timezone=appointment.timezone,
            ),
            template="appointment_confirmation",
            idempotency_key=f"appt:{appointment_id}:sms",
            provider=deps.sms,
        )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_sentry(settings, service="worker-confirmations")
    if settings.app_env in ("staging", "production"):
        await ensure_rls_enforced()
    redis = worker_redis(settings.redis_url)
    stop = stop_on_signal()
    deps = Deps(
        settings=settings,
        email=get_email_provider(settings),
        sms=get_sms_provider(settings),
    )
    await reclaim_orphans(redis, CONFIRMATIONS_QUEUE)
    logger.info("confirmations_worker_started", queue=CONFIRMATIONS_QUEUE)
    try:
        await run_worker(
            redis,
            CONFIRMATIONS_QUEUE,
            functools.partial(handle_confirmation, deps=deps),
            stop=stop,
        )
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
