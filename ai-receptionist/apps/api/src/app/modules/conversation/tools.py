"""In-call tool executor: dispatches Vapi tool-call requests to the right
handler and returns spoken-language results while the caller waits (Phase 1
§3.3 targets <2s per tool).

Some tools do call out synchronously — `answer_faq` (embedding + LLM) and the
calendar tools. The two read-only ones run under a hard deadline
(`read_deadline_seconds`) and fall back to a spoken message rather than leave
the caller in silence. `book_appointment` is deliberately *not* run under one:
cancelling it mid-flight would skip its release-on-failure path (cancellation
is not an `Exception`) and could strand a reserved slot, so it relies on the
calendar client's own short timeout instead.

TOOL_SCHEMAS follows the standard function-calling shape (type/function/
name/description/parameters) Vapi's docs describe; not yet contract-tested
against the live sandbox — verify alongside the Vapi adapter during the M1
live-call test.
"""

import asyncio
import copy
import re
import uuid
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.modules.conversation.faq import NO_ANSWER_FALLBACK, answer_faq
from app.modules.conversation.providers.base import EmbeddingProvider, LLMProvider
from app.modules.conversation.qualification import record_answer
from app.modules.escalation.service import create_callback_request
from app.modules.scheduling.providers.base import CalendarProvider
from app.modules.scheduling.tools import (
    COULD_NOT_CHECK,
    handle_book_appointment,
    handle_check_availability,
)
from app.modules.tenants.models import AgentConfig, Tenant

logger = get_logger(__name__)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "answer_faq",
            "description": (
                "Answer a caller's question using the business's knowledge base. "
                "Use this for any factual question about the business (hours, "
                "services, prices, policies, location) instead of answering from "
                "memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_qualification_answer",
            "description": "Record the caller's answer to one qualification question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The qualification question key"},
                    "answer": {"description": "The caller's answer"},
                },
                "required": ["key", "answer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": (
                "Find open appointment times. Call this before offering any "
                "time to the caller — never guess or invent availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "What the caller wants to come in for",
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": (
                            "The caller's preferred date/time in the business's local "
                            "time as ISO 8601, e.g. 2026-09-15T09:00. Omit if they "
                            "have no preference."
                        ),
                    },
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Book a specific time that check_availability offered. Confirm "
                "the time and the caller's name back to them first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "starts_at": {
                        "type": "string",
                        "description": (
                            "Start time in the business's local time as ISO 8601, "
                            "e.g. 2026-09-15T09:00"
                        ),
                    },
                    "customer_name": {"type": "string"},
                    "customer_email": {
                        "type": "string",
                        "description": "Optional — ask for it to send an email confirmation",
                    },
                },
                "required": ["service", "starts_at", "customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_callback",
            "description": (
                "Take a voicemail/callback request when the caller asks for a "
                "human, asks something outside what you can help with, or needs "
                "escalation. Collect the caller's name and reason first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "preferred_window": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
]


# Keys the booking tool already uses, which a tenant's booking_fields must not
# shadow, and the shape a field key must have to be a safe JSON-schema property.
_CORE_BOOKING_KEYS = {"service", "starts_at", "customer_name", "customer_email"}
_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def booking_fields(tenant: Tenant) -> list[dict[str, str]]:
    """The extra details this business collects when booking
    (`settings.scheduling.booking_fields`), e.g. an auto shop's vehicle and
    mileage. Malformed or clashing entries are dropped rather than sent to the
    voice vendor as a broken tool schema."""
    scheduling = (tenant.settings or {}).get("scheduling", {}) or {}
    fields = []
    for entry in scheduling.get("booking_fields", []) or []:
        key = str((entry or {}).get("key", ""))
        if not _FIELD_KEY.match(key) or key in _CORE_BOOKING_KEYS:
            continue
        description = str(entry.get("description") or entry.get("label") or key)
        fields.append({"key": key, "description": description})
    return fields


def tool_schemas_for(tenant: Tenant) -> list[dict[str, Any]]:
    """TOOL_SCHEMAS specialised for one tenant: its service names in the
    scheduling tools (so the model picks a real service, and the duration
    lookup matches) and its booking_fields as optional booking parameters."""
    schemas = copy.deepcopy(TOOL_SCHEMAS)
    scheduling = (tenant.settings or {}).get("scheduling", {}) or {}
    services = [str(s.get("name")) for s in scheduling.get("services", []) or [] if s.get("name")]
    for schema in schemas:
        function = schema["function"]
        properties = function["parameters"]["properties"]
        if services and function["name"] in ("check_availability", "book_appointment"):
            properties["service"]["description"] = "One of: " + "; ".join(services)
        if function["name"] == "book_appointment":
            for field in booking_fields(tenant):
                properties[field["key"]] = {"type": "string", "description": field["description"]}
    return schemas


class ToolExecutor:
    def __init__(
        self,
        llm: LLMProvider,
        embeddings: EmbeddingProvider,
        calendar: CalendarProvider | None = None,
        redis: Redis | None = None,
        read_deadline_seconds: float = 8.0,
        send_confirmations: bool = True,
        booking_scope: str | None = None,
    ) -> None:
        self._read_deadline = read_deadline_seconds
        # False for test-lab sessions: a simulated booking is real, but its
        # caller number is made up, so nothing is texted or emailed to it.
        self._send_confirmations = send_confirmations
        # Replaces the call as the booking's retry identity. The test lab's
        # tool bench sets a fresh one per run: each run is a deliberate new
        # booking, not a vendor retry of the last one.
        self._booking_scope = booking_scope
        self._llm = llm
        self._embeddings = embeddings
        # Optional so non-scheduling deployments (and unit tests) need no
        # calendar wiring; the scheduling tools report unavailable instead.
        self._calendar = calendar
        self._redis = redis

    async def dispatch(
        self,
        *,
        tenant: Tenant,
        call_id: uuid.UUID,
        caller_e164: str,
        agent_config: AgentConfig,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        tenant_id = tenant.id
        try:
            match tool_name:
                case "answer_faq":
                    return await self._within_deadline(
                        tool_name,
                        answer_faq(
                            tenant_id,
                            call_id,
                            arguments.get("question", ""),
                            self._llm,
                            self._embeddings,
                        ),
                        fallback=NO_ANSWER_FALLBACK,
                    )
                case "check_availability":
                    if self._calendar is None:
                        return COULD_NOT_CHECK
                    return await self._within_deadline(
                        tool_name,
                        handle_check_availability(
                            tenant=tenant,
                            calendar=self._calendar,
                            service=arguments.get("service", ""),
                            preferred_time=arguments.get("preferred_time"),
                        ),
                        fallback=COULD_NOT_CHECK,
                    )
                case "book_appointment":
                    if self._calendar is None or self._redis is None:
                        return COULD_NOT_CHECK
                    return await handle_book_appointment(
                        tenant=tenant,
                        calendar=self._calendar,
                        redis=self._redis,
                        call_id=call_id,
                        service=arguments.get("service", ""),
                        starts_at=arguments.get("starts_at", ""),
                        customer_name=arguments.get("customer_name", ""),
                        customer_phone=caller_e164,
                        customer_email=arguments.get("customer_email"),
                        details={
                            f["key"]: str(arguments[f["key"]])
                            for f in booking_fields(tenant)
                            if arguments.get(f["key"])
                        },
                        send_confirmation=self._send_confirmations,
                        idempotency_scope=self._booking_scope,
                    )
                case "record_qualification_answer":
                    lead_id, score = await record_answer(
                        tenant_id,
                        call_id,
                        caller_e164,
                        arguments.get("key", ""),
                        arguments.get("answer"),
                        agent_config,
                    )
                    logger.info("qualification_answer_recorded", lead_id=str(lead_id), score=score)
                    return "Got it, thank you."
                case "request_callback":
                    request_id = await create_callback_request(
                        tenant_id=tenant_id,
                        call_id=call_id,
                        caller_e164=caller_e164,
                        reason=arguments.get("reason", ""),
                        preferred_window=arguments.get("preferred_window"),
                    )
                    logger.info("callback_requested", request_id=str(request_id))
                    return cast(
                        str,
                        agent_config.escalation_policy.get(
                            "callback_promise", "Someone from the team will call you back."
                        ),
                    )
                case _:
                    logger.warning("unknown_tool_call", tool_name=tool_name)
                    return "I'm not able to do that right now, but I can take a message."
        except Exception:
            logger.exception("tool_dispatch_failed", tool_name=tool_name, tenant_id=str(tenant_id))
            return "Sorry, I had trouble with that — let me note it down for the team."

    async def _within_deadline(self, tool_name: str, work: Awaitable[str], *, fallback: str) -> str:
        try:
            async with asyncio.timeout(self._read_deadline):
                return await work
        except TimeoutError:
            logger.warning(
                "tool_deadline_exceeded", tool_name=tool_name, deadline_seconds=self._read_deadline
            )
            return fallback
