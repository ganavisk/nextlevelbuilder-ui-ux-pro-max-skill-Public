"""ORM models owned by the tenants module: tenants, phone numbers, agent
configs, dashboard users. Schema is defined in migrations; these map to it."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    vertical: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="America/New_York")
    business_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    # Is the voice vendor's agent up to date? See migration 0009 and
    # tenants/voice_sync.py.
    voice_sync_requested: Mapped[int] = mapped_column(Integer, default=0)
    voice_sync_done: Mapped[int] = mapped_column(Integer, default=0)
    voice_sync_failed_version: Mapped[int] = mapped_column(Integer, default=0)
    voice_sync_error: Mapped[str] = mapped_column(Text, default="")
    voice_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    """A dashboard login, scoped to one tenant. `role` is a flat hierarchy —
    owner > admin > viewer — checked by `core.auth.require_role`."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(Text, default="viewer")
    password_hash: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    e164: Mapped[str] = mapped_column(Text, unique=True)
    vendor: Mapped[str] = mapped_column(Text, default="vapi")
    vendor_number_id: Mapped[str] = mapped_column(Text, default="")
    vendor_agent_id: Mapped[str] = mapped_column(Text, default="")


class Integration(Base):
    """A tenant's connection to a third-party service. Credentials are Fernet
    ciphertext — the key lives only in the environment, never the database."""

    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(Text)
    credentials_encrypted: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="connected")


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    version: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    system_prompt: Mapped[str] = mapped_column(Text)
    first_message: Mapped[str] = mapped_column(Text)
    voice_id: Mapped[str] = mapped_column(Text, default="")
    # "" means the voice vendor's default; e.g. "11labs" for ElevenLabs voices.
    voice_provider: Mapped[str] = mapped_column(Text, default="")
    voice_model: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(Text, default="en")
    qualification: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    escalation_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=lambda: {"mode": "voicemail_callback"}
    )
