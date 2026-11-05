"""ORM model owned by the scheduling module."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    service: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    external_event_id: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="confirmed")
    # Unique when present — a retried booking returns the original row
    # rather than creating a second appointment (migration 0005).
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name: Mapped[str] = mapped_column(Text, default="")
    customer_phone: Mapped[str] = mapped_column(Text, default="")
    customer_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, default="UTC")
    # Which of the tenant's parallel resources this occupies (migration 0008).
    bay: Mapped[int] = mapped_column(SmallInteger, default=1)
    # Booking-time answers the tenant asked to collect (scheduling.booking_fields).
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
