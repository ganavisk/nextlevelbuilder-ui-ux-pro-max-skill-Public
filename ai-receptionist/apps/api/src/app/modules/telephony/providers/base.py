"""VoiceProvider — the boundary between our platform and the voice vendor.

Purpose: everything vendor-specific (Vapi today, Retell as fallback) lives
behind this interface. Swapping vendors means writing one new adapter; the
conversation engine, tool executor, and data layer never change.

Inputs: raw webhook payloads and our tenant-level agent configuration.
Outputs: normalized platform events and vendor-side agent/call operations.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CallEventType(StrEnum):
    CALL_STARTED = "call_started"
    CALL_ENDED = "call_ended"
    TOOL_CALL = "tool_call"
    TRANSCRIPT_UPDATE = "transcript_update"
    STATUS_UPDATE = "status_update"


@dataclass(frozen=True)
class NormalizedCallEvent:
    """Vendor-agnostic representation of a voice webhook event."""

    event_type: CallEventType
    vendor_call_id: str
    to_number: str  # tenant's number (E.164) — resolves the tenant
    from_number: str  # caller (E.164)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool invocation extracted from a TOOL_CALL event. A single event
    may carry several of these (a model turn can request multiple tools)."""

    id: str
    name: str
    arguments: dict[str, Any]


def clock_instructions(*, now: str, today: str, timezone: str) -> str:
    """Tell the model what time it is at the business.

    Without this the model has no idea what "tomorrow" or "next Tuesday" is,
    and the scheduling tools take an absolute local ISO time it has to work
    out. A vendor agent's prompt is provisioned once and reused for every
    call, so `now` and `today` can't be baked in as values: each adapter
    passes its own per-call template (see VapiProvider). The Test Lab passes
    real values. Both use this one wording.
    """
    return (
        f"Current local time at the business: {now} ({timezone}). Work out dates the "
        f'caller gives relative to it ("tomorrow", "next Tuesday"), and give times to '
        f"tools in local ISO 8601, e.g. {today}T09:00."
    )


@dataclass(frozen=True)
class AgentDefinition:
    """What the vendor needs to run a tenant's agent: prompt, voice, tools.

    `system_prompt` is time-independent. The adapter appends the current
    local time per call (`clock_instructions`) in the vendor's own template
    syntax, using `timezone`."""

    tenant_id: uuid.UUID
    system_prompt: str
    first_message: str
    voice_id: str
    # The business's IANA timezone: the clock the agent is told it is.
    timezone: str
    # Empty means "vendor default". `voice_provider` names the TTS vendor
    # (e.g. "11labs"), so a tenant can use ElevenLabs voices while the
    # voice-agent orchestration stays with the primary vendor.
    voice_provider: str = ""
    voice_model: str = ""
    language: str = "en"
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_duration_seconds: int = 900


class VoiceProvider(ABC):
    """Adapter interface for voice-agent vendors."""

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> None:
        """Raise WebhookSignatureError unless the request is authentic."""

    @abstractmethod
    def parse_event(self, payload: dict[str, Any]) -> NormalizedCallEvent:
        """Translate a vendor webhook body into a NormalizedCallEvent."""

    @abstractmethod
    async def sync_agent(self, definition: AgentDefinition, *, existing_agent_id: str = "") -> str:
        """Create or update the vendor-side agent; return the vendor agent id.

        Must be idempotent: pass the previously stored id to update in place
        rather than creating a duplicate agent on every provisioning run.
        """

    @abstractmethod
    async def attach_number(self, vendor_agent_id: str, e164_number: str) -> str:
        """Route an inbound phone number to the given vendor agent; return the
        vendor's id for that number. Idempotent — re-pointing an already
        imported number must not fail or duplicate it."""

    @abstractmethod
    def extract_tool_calls(self, event: NormalizedCallEvent) -> list[ToolCallRequest]:
        """Pull the individual tool invocations out of a TOOL_CALL event."""

    @abstractmethod
    def format_tool_results(self, results: list[tuple[str, str]]) -> dict[str, Any]:
        """Shape (tool_call_id, result_text) pairs the way the vendor expects
        them in the webhook response, so they can be spoken to the caller."""
