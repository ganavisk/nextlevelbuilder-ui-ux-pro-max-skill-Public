"""Notification dispatch: claim a row, then send.

The ordering is the whole design. Each notification is inserted `pending`
with a unique idempotency key *before* the provider is called, and the send
only proceeds if this worker won that insert. A retried job therefore finds
the row already claimed and stops — a customer never receives the same
confirmation twice, even if the queue redelivers.

Providers are called here and nowhere else on the async path; nothing in this
module runs while a caller is waiting.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import table_of, tenant_session
from app.core.logging import get_logger
from app.modules.notifications.models import Notification
from app.modules.notifications.providers.base import EmailProvider, SMSProvider

logger = get_logger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    sent: bool
    reason: str = ""


async def _claim(
    *,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID | None,
    call_id: uuid.UUID | None,
    channel: str,
    recipient: str,
    template: str,
    idempotency_key: str,
) -> uuid.UUID | None:
    """Insert the pending row. Returns its id, or None if another worker
    already claimed this exact notification."""
    async with tenant_session(tenant_id) as session:
        stmt = (
            pg_insert(Notification)
            .values(
                tenant_id=tenant_id,
                appointment_id=appointment_id,
                call_id=call_id,
                channel=channel,
                recipient=recipient,
                template=template,
                idempotency_key=idempotency_key,
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Notification.id)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def _finalize(
    tenant_id: uuid.UUID, notification_id: uuid.UUID, *, status: str, message_id: str = ""
) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(Notification)
            .update()
            .where(Notification.id == notification_id)
            .values(
                status=status,
                provider_message_id=message_id or None,
                sent_at=datetime.now(UTC) if status == "sent" else None,
            )
        )


async def send_email(
    *,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID | None,
    call_id: uuid.UUID | None,
    to: str,
    subject: str,
    html: str,
    from_address: str,
    template: str,
    idempotency_key: str,
    provider: EmailProvider,
) -> DispatchResult:
    if not to:
        return DispatchResult(sent=False, reason="no_recipient")

    notification_id = await _claim(
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        call_id=call_id,
        channel="email",
        recipient=to,
        template=template,
        idempotency_key=idempotency_key,
    )
    if notification_id is None:
        return DispatchResult(sent=False, reason="already_claimed")

    try:
        message_id = await provider.send(
            to=to,
            subject=subject,
            html=html,
            from_address=from_address,
            idempotency_key=idempotency_key,
        )
    except Exception:
        await _finalize(tenant_id, notification_id, status="failed")
        logger.exception("email_send_failed", tenant_id=str(tenant_id), template=template)
        raise

    await _finalize(tenant_id, notification_id, status="sent", message_id=message_id)
    logger.info("email_sent", template=template, notification_id=str(notification_id))
    return DispatchResult(sent=True)


async def send_sms(
    *,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID | None,
    call_id: uuid.UUID | None,
    to: str,
    body: str,
    template: str,
    idempotency_key: str,
    provider: SMSProvider,
) -> DispatchResult:
    if not to:
        return DispatchResult(sent=False, reason="no_recipient")

    notification_id = await _claim(
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        call_id=call_id,
        channel="sms",
        recipient=to,
        template=template,
        idempotency_key=idempotency_key,
    )
    if notification_id is None:
        return DispatchResult(sent=False, reason="already_claimed")

    try:
        message_id = await provider.send(to=to, body=body, idempotency_key=idempotency_key)
    except Exception:
        await _finalize(tenant_id, notification_id, status="failed")
        logger.exception("sms_send_failed", tenant_id=str(tenant_id), template=template)
        raise

    await _finalize(tenant_id, notification_id, status="sent", message_id=message_id)
    logger.info("sms_sent", template=template, notification_id=str(notification_id))
    return DispatchResult(sent=True)


async def pending_for_appointment(
    tenant_id: uuid.UUID, appointment_id: uuid.UUID
) -> list[Notification]:
    async with tenant_session(tenant_id) as session:
        return list(
            (
                await session.execute(
                    select(Notification).where(Notification.appointment_id == appointment_id)
                )
            )
            .scalars()
            .all()
        )
