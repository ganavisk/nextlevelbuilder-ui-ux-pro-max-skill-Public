"""ORM model owned by the escalation module: the voicemail-plus-callback
queue (Phase 1's v1 escalation policy — no live transfer)."""

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CallbackRequest(Base):
    __tablename__ = "callback_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    caller_e164: Mapped[str] = mapped_column(Text)
    # A different number the caller asked to be called back on; None means
    # call `caller_e164` (migration 0010).
    callback_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    voicemail_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_window: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="open")
