"""Pydantic response shapes for the dashboard's callback queue."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CallbackRequestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    caller_e164: str
    # Dial this instead of caller_e164 when set.
    callback_e164: str | None
    reason: str
    preferred_window: str | None
    status: str


class CallbackStatusUpdate(BaseModel):
    status: str
