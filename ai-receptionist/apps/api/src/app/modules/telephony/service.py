"""Call lifecycle: the vendor-agnostic core of the telephony module.

Receives NormalizedCallEvents (vendor already stripped away by the adapter)
and owns: tenant resolution, call persistence, Redis call-session state, and
handing finished calls to the async post-call queue. The hot path here does
no LLM work and no integration I/O — that is the latency budget rule from
Phase 1 §3.3.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import Settings
from app.core.db import tenant_session
from app.core.logging import get_logger
from app.core.queue import POST_CALL_QUEUE, enqueue
from app.modules.telephony.models import Call, CallEvent, Transcript
from app.modules.telephony.providers.base import CallEventType, NormalizedCallEvent
from app.modules.tenants.repository import resolve_tenant_by_number

logger = get_logger(__name__)

CALL_STATE_PREFIX = "call:"


class TelephonyService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def handle_event(self, event: NormalizedCallEvent) -> dict[str, Any]:
        logger.info(
            "voice_event",
            event_type=event.event_type,
            vendor_call_id=event.vendor_call_id,
            to_number=event.to_number,
        )
        match event.event_type:
            case CallEventType.CALL_STARTED:
                return await self._on_call_started(event)
            case CallEventType.CALL_ENDED:
                return await self._on_call_ended(event)
            case _:
                return await self._on_progress_event(event)

    # ── lifecycle handlers ─────────────────────────────────────────────────

    async def _on_call_started(self, event: NormalizedCallEvent) -> dict[str, Any]:
        await self._register_call(event)
        return {"status": "accepted"}

    async def _register_call(self, event: NormalizedCallEvent) -> tuple[uuid.UUID, uuid.UUID]:
        """Create the call row (idempotently) and its Redis session state.

        Normally run by the call-started event. Also run when a later event
        arrives for a call we have no record of: vendors don't guarantee
        webhook order or delivery, and a lost or late start must not turn
        every tool call on that call into a 500 the caller hears as silence.
        """
        tenant, number = await resolve_tenant_by_number(event.to_number)
        async with tenant_session(tenant.id) as session:
            # Idempotent: vendors retry webhooks; (tenant_id, vendor_call_id) is unique.
            await session.execute(
                pg_insert(Call)
                .values(
                    tenant_id=tenant.id,
                    phone_number_id=number.id,
                    vendor_call_id=event.vendor_call_id,
                    caller_e164=event.from_number,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "vendor_call_id"])
            )
            call_id = (
                await session.execute(
                    select(Call.id).where(
                        Call.tenant_id == tenant.id, Call.vendor_call_id == event.vendor_call_id
                    )
                )
            ).scalar_one()

        await self._redis.set(
            f"{CALL_STATE_PREFIX}{event.vendor_call_id}",
            json.dumps({"tenant_id": str(tenant.id), "call_id": str(call_id)}),
            ex=self._settings.max_call_duration_seconds + 600,
        )
        return tenant.id, call_id

    async def _on_call_ended(self, event: NormalizedCallEvent) -> dict[str, Any]:
        tenant_id, call_id = await self._resolve_call(event)
        artifact = event.payload.get("artifact") or {}
        cost_cents = round(float(event.payload.get("cost") or 0) * 100)

        async with tenant_session(tenant_id) as session:
            # Every write here tolerates a vendor redelivery: a retried
            # end-of-call report must converge on the same state, not 500.
            await session.execute(
                update(Call)
                .where(Call.id == call_id)
                .values(
                    ended_at=func.coalesce(Call.ended_at, datetime.now(UTC)),
                    recording_url=artifact.get("recordingUrl"),
                    vendor_cost_cents=cost_cents,
                )
            )
            await session.execute(
                pg_insert(Transcript)
                .values(tenant_id=tenant_id, call_id=call_id, turns=self._normalize_turns(artifact))
                .on_conflict_do_nothing(index_elements=["call_id"])
            )
            session.add(
                CallEvent(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    event_type="call_ended",
                    payload={"cost": event.payload.get("cost")},
                )
            )

        # Enqueued on every delivery, not only the first: if the enqueue
        # fails after the commit above, the vendor's retry must be able to
        # enqueue it. The consumer is idempotent (post_call skips a call that
        # already has an outcome), so a duplicate job costs nothing.
        await enqueue(
            self._redis,
            POST_CALL_QUEUE,
            {"type": "post_call", "tenant_id": str(tenant_id), "call_id": str(call_id)},
        )
        await self._redis.delete(f"{CALL_STATE_PREFIX}{event.vendor_call_id}")
        return {"status": "accepted"}

    async def _on_progress_event(self, event: NormalizedCallEvent) -> dict[str, Any]:
        # TOOL_CALL events are dispatched to the conversation module by the
        # route (which needs the tool result to build the HTTP response);
        # this still records the audit trail for every progress event.
        tenant_id, call_id = await self._resolve_call(event)
        async with tenant_session(tenant_id) as session:
            session.add(
                CallEvent(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    event_type=event.event_type,
                    payload={"vendor_type": event.payload.get("type")},
                )
            )
        return {"status": "accepted"}

    # ── public: used by the webhook route to dispatch tool calls ───────────

    async def resolve_call_context(self, event: NormalizedCallEvent) -> tuple[uuid.UUID, uuid.UUID]:
        """(tenant_id, call_id) for an in-flight call. Exposed so the route
        can resolve identity for tool dispatch without duplicating the
        Redis-then-DB fallback logic in `_resolve_call`."""
        return await self._resolve_call(event)

    # ── helpers ────────────────────────────────────────────────────────────

    async def _resolve_call(self, event: NormalizedCallEvent) -> tuple[uuid.UUID, uuid.UUID]:
        """Prefer the Redis call-session state; fall back to DB resolution so
        a Redis flush mid-call can never lose the end-of-call report; and if
        the call isn't in the DB either, register it from this event."""
        raw = await self._redis.get(f"{CALL_STATE_PREFIX}{event.vendor_call_id}")
        if raw:
            state = json.loads(raw)
            return uuid.UUID(state["tenant_id"]), uuid.UUID(state["call_id"])

        tenant, _ = await resolve_tenant_by_number(event.to_number)
        async with tenant_session(tenant.id) as session:
            call_id = (
                await session.execute(
                    select(Call.id).where(
                        Call.tenant_id == tenant.id, Call.vendor_call_id == event.vendor_call_id
                    )
                )
            ).scalar_one_or_none()
        if call_id is not None:
            return tenant.id, call_id
        # The start event never arrived (or hasn't yet): register the call now.
        logger.warning(
            "call_registered_late",
            vendor_call_id=event.vendor_call_id,
            event_type=event.event_type,
        )
        return await self._register_call(event)

    @staticmethod
    def _normalize_turns(artifact: dict[str, Any]) -> list[dict[str, Any]]:
        turns = []
        for message in artifact.get("messages") or []:
            role = message.get("role", "")
            if role in ("bot", "assistant"):
                role = "assistant"
            elif role in ("user", "customer", "human"):
                role = "caller"
            else:
                continue  # system/tool frames are not part of the spoken transcript
            turns.append(
                {"role": role, "text": message.get("message", ""), "ts": message.get("time")}
            )
        return turns
