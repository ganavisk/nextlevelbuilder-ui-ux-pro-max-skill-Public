"""Pydantic response shapes for the dashboard's call list/detail views."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CallSummary(BaseModel):
    """One row in the call list — no transcript, no per-turn detail; that's
    the whole reason it's a separate schema from CallDetail rather than one
    model with optional fields. A list of 50 calls should not carry 50
    transcripts over the wire to render a table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    caller_e164: str
    direction: str
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    sentiment: str | None
    summary: str | None


class TranscriptTurn(BaseModel):
    role: str
    text: str


class CallDetail(CallSummary):
    recording_url: str | None
    vendor_cost_cents: int
    llm_cost_cents: int
    transcript: list[TranscriptTurn]
