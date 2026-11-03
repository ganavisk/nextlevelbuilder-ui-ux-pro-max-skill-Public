"""Request/response shapes for the Test Lab API."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class LabStatus(BaseModel):
    llm_available: bool
    tools: list[str]
    bays: int
    timezone: str
    default_caller: str
    services: list[str]
    booking_fields: list[dict[str, str]]


class StartSessionRequest(BaseModel):
    caller_e164: str | None = Field(default=None, max_length=20)


class SessionOut(BaseModel):
    session_id: UUID
    caller_e164: str
    first_message: str
    started_at: datetime


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ToolEventOut(BaseModel):
    tool: str
    arguments: dict[str, Any]
    result: str
    latency_ms: int


class TurnOut(BaseModel):
    reply: str
    tool_events: list[ToolEventOut]
    model: str
    input_tokens: int
    output_tokens: int


class ToolRunRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class DiscardOut(BaseModel):
    removed_appointments: int


class BoardAppointmentOut(BaseModel):
    id: UUID
    bay: int
    starts_at: datetime
    ends_at: datetime
    service: str
    customer_name: str
    details: dict[str, Any]
    simulated: bool


class BoardOut(BaseModel):
    day: date
    timezone: str
    bays: int
    opening_hours: list[list[str]]
    appointments: list[BoardAppointmentOut]


class ActivityOut(BaseModel):
    id: UUID
    occurred_at: datetime
    event_type: str
    call_id: UUID
    caller_e164: str
    simulated: bool
    tool: str | None
    summary: str
    latency_ms: int | None
