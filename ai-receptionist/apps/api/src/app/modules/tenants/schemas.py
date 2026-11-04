"""Pydantic request/response shapes for the tenants module's dashboard API.

Kept separate from the ORM models (models.py) on purpose: a response schema
is a public contract (the dashboard's typed client is generated from it,
Phase 1 §3.6) and must change deliberately, while the ORM model is free to
grow internal-only columns without that automatically becoming API surface.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    tenant_slug: str
    email: str
    password: str


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str
    role: str


class TenantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    vertical: str
    timezone: str
    business_hours: dict[str, Any]
    settings: dict[str, Any]
    status: str
    created_at: datetime


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant: TenantSummary
    user: UserSummary


class TenantUpdateRequest(BaseModel):
    """All fields optional: PATCH semantics, only supplied fields change.
    `settings` is merged over the existing value in routes.py rather than
    replacing it wholesale — a caller updating `settings.crm` shouldn't be
    required to resend `settings.scheduling` and `settings.notifications`
    just to avoid erasing them."""

    timezone: str | None = None
    business_hours: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class VoiceSyncSummary(BaseModel):
    """Whether the phone agent callers reach reflects the saved settings.

    live: it does. updating: a save is being pushed. failed: the last push
    failed (`error` says why; POST /tenants/me/voice-sync retries).
    not_connected: no phone number is provisioned yet (`make provision`)."""

    state: Literal["live", "updating", "failed", "not_connected"]
    synced_at: datetime | None
    error: str


class AgentConfigSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
    is_active: bool
    system_prompt: str
    first_message: str
    voice_id: str
    voice_provider: str
    voice_model: str
    language: str
    qualification: list[Any]
    escalation_policy: dict[str, Any]


class CallbackPromisesUpdate(BaseModel):
    """The two escalation-policy texts the dashboard edits. Merged into the
    current policy, so keys the form doesn't show (mode, triggers) survive."""

    callback_promise: str
    after_hours_promise: str = ""


class CallbackTriggersUpdate(BaseModel):
    """The situations in which the receptionist takes a callback instead of
    handling the request itself. Merged into the current escalation policy."""

    triggers: list[str]


class CallbackPromisesPreview(BaseModel):
    """For the Settings preview: what {next_open} says for a caller who rang
    after today's closing, from the real business hours (None without hours)."""

    next_open_example: str | None


class AgentConfigUpdateRequest(BaseModel):
    """All fields optional; omitted fields carry over from the current
    active version (see repository.create_agent_config_version) rather than
    being cleared — a dashboard form editing only the persona shouldn't be
    able to silently wipe the qualification flow by not resending it."""

    system_prompt: str | None = None
    first_message: str | None = None
    voice_id: str | None = None
    voice_provider: str | None = None
    voice_model: str | None = None
    language: str | None = None
    qualification: list[Any] | None = None
    escalation_policy: dict[str, Any] | None = None


class IntegrationSummary(BaseModel):
    provider: str
    status: str
    connected: bool


class ConnectUrlResponse(BaseModel):
    authorize_url: str


class MessageResponse(BaseModel):
    """Generic {"message": "..."} envelope for endpoints with no natural
    resource to return (e.g. a cancellation, a status transition)."""

    message: str
