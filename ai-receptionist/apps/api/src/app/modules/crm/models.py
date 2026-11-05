"""ORM models owned by the crm module. Schema lives in migration 0006."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CrmSync(Base):
    """One CRM synchronization attempt-set for one call.

    The three external id columns are also the resume points: a non-null
    value means that step completed and must not be repeated, because the
    vendor APIs have no idempotency key of their own.
    """

    __tablename__ = "crm_syncs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id"), nullable=True
    )
    provider: Mapped[str] = mapped_column(Text, default="hubspot")
    contact_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    deal_external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def pending_steps(self) -> dict[str, Any]:
        """Which steps still need doing — used for logging and assertions."""
        return {
            "contact": self.contact_external_id is None,
            "activity": self.activity_external_id is None,
            "deal": self.deal_external_id is None,
        }
