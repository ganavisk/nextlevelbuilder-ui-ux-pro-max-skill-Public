"""Test Lab: rehearse calls without a phone.

A simulated call runs the tenant's *real* agent (the same assembled system
prompt and tools `make provision` sends to the voice vendor) against Claude,
and executes every tool call through the same ToolExecutor and database the
live webhook uses. What differs is only what would reach the outside world:

- the call is stored with direction 'simulated', which keeps it out of
  analytics and CRM sync;
- bookings are real (so the schedule can be tested) but send no text or
  email, since the caller number is made up;
- `discard_session` removes everything a session created, cancelling any
  calendar events it wrote.

The model's message history (Anthropic content blocks, including tool_use and
tool_result) lives in Redis for the session's lifetime; the human-readable
transcript and a trace of every tool call are written to the database.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import tenant_session
from app.core.errors import AppError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.modules.conversation.models import Lead
from app.modules.conversation.providers.base import CompletionRequest, LLMProvider
from app.modules.conversation.tools import ToolExecutor, tool_schemas_for
from app.modules.escalation.models import CallbackRequest
from app.modules.scheduling.availability import WEEKDAY_KEYS
from app.modules.scheduling.models import Appointment
from app.modules.scheduling.providers.base import CalendarProvider
from app.modules.scheduling.service import bay_count, cancel_appointment
from app.modules.telephony.models import Call, CallEvent, Transcript
from app.modules.tenants.models import AgentConfig, Tenant
from app.modules.tenants.service import assemble_system_prompt

logger = get_logger(__name__)

SIMULATED = "simulated"
DEFAULT_TEST_CALLER = "+15555550123"
MAX_TOOL_ROUNDS = 5
HISTORY_TTL_SECONDS = 4 * 3600
TOOL_NAMES = (
    "answer_faq",
    "check_availability",
    "book_appointment",
    "request_callback",
    "record_qualification_answer",
)


class LabLLMUnavailableError(AppError):
    """The model behind simulated calls could not be reached (usually: no
    ANTHROPIC_API_KEY). The tool bench still works without it."""

    status_code = 503
    code = "llm_unavailable"


@dataclass
class ToolEvent:
    tool: str
    arguments: dict[str, Any]
    result: str
    latency_ms: int


@dataclass
class TurnResult:
    reply: str
    tool_events: list[ToolEvent] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _history_key(call_id: uuid.UUID) -> str:
    return f"lab:history:{call_id}"


def anthropic_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The provisioning schemas are in the function-calling shape the voice
    vendor takes; Claude's Messages API wants name/description/input_schema."""
    return [
        {
            "name": s["function"]["name"],
            "description": s["function"].get("description", ""),
            "input_schema": s["function"]["parameters"],
        }
        for s in schemas
    ]


def lab_system_prompt(tenant: Tenant, config: AgentConfig, *, now: datetime, greeting: str) -> str:
    """The production prompt, plus the context a phone call would supply:
    what was already said, and today's date (the model needs it to turn
    'Tuesday morning' into the ISO time the scheduling tools take)."""
    local = now.astimezone(ZoneInfo(tenant.timezone))
    return (
        assemble_system_prompt(tenant, config)
        + "\n\nThis is a phone call; keep replies short and conversational, no lists or "
        + "markdown.\n"
        + f'You already greeted the caller with: "{greeting}"\n'
        + f"Current local time at the business: {local:%A, %B %d, %Y, %I:%M %p} "
        + f"({tenant.timezone}). Give times to tools in local ISO 8601, e.g. "
        + f"{local:%Y-%m-%d}T09:00."
    )


# ── sessions ────────────────────────────────────────────────────────────


async def start_session(
    tenant: Tenant, config: AgentConfig, *, caller_e164: str, redis: Redis
) -> Call:
    call_id = uuid.uuid4()
    async with tenant_session(tenant.id) as session:
        call = Call(
            id=call_id,
            tenant_id=tenant.id,
            vendor_call_id=f"sim-{call_id}",
            caller_e164=caller_e164 or DEFAULT_TEST_CALLER,
            direction=SIMULATED,
        )
        session.add(call)
        session.add(
            Transcript(
                tenant_id=tenant.id,
                call_id=call_id,
                turns=[{"role": "assistant", "text": config.first_message, "ts": _now_iso()}],
            )
        )
        await session.flush()
        session.expunge(call)
    await redis.set(_history_key(call_id), "[]", ex=HISTORY_TTL_SECONDS)
    logger.info("lab_session_started", tenant_id=str(tenant.id), call_id=str(call_id))
    return call


async def get_session(tenant_id: uuid.UUID, call_id: uuid.UUID) -> Call:
    async with tenant_session(tenant_id) as session:
        call = (
            await session.execute(
                select(Call).where(
                    Call.id == call_id, Call.tenant_id == tenant_id, Call.direction == SIMULATED
                )
            )
        ).scalar_one_or_none()
        if call is None:
            raise NotFoundError(f"no test session {call_id}")
        session.expunge(call)
        return call


def _executor(
    *,
    llm: LLMProvider,
    embeddings: Any,
    calendar: CalendarProvider | None,
    redis: Redis,
    deadline_seconds: float,
) -> ToolExecutor:
    return ToolExecutor(
        llm,
        embeddings,
        calendar=calendar,
        redis=redis,
        read_deadline_seconds=deadline_seconds,
        send_confirmations=False,
    )


async def _run_tool(
    executor: ToolExecutor,
    *,
    tenant: Tenant,
    call: Call,
    config: AgentConfig,
    tool: str,
    arguments: dict[str, Any],
) -> ToolEvent:
    started = time.perf_counter()
    result = await executor.dispatch(
        tenant=tenant,
        call_id=call.id,
        caller_e164=call.caller_e164,
        agent_config=config,
        tool_name=tool,
        arguments=arguments,
    )
    event = ToolEvent(
        tool=tool,
        arguments=arguments,
        result=result,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )
    async with tenant_session(tenant.id) as session:
        session.add(
            CallEvent(
                tenant_id=tenant.id,
                call_id=call.id,
                event_type="tool_call",
                payload={
                    "tool": tool,
                    "arguments": arguments,
                    "result": result,
                    "latency_ms": event.latency_ms,
                    "source": "test_lab",
                },
            )
        )
    return event


async def run_bench_tool(
    *,
    tenant: Tenant,
    config: AgentConfig,
    call: Call,
    tool: str,
    arguments: dict[str, Any],
    llm: LLMProvider,
    embeddings: Any,
    calendar: CalendarProvider | None,
    redis: Redis,
    deadline_seconds: float,
) -> ToolEvent:
    """Run one tool directly, no model involved: the deterministic way to test
    scheduling (and the only way without an LLM key)."""
    if call.ended_at is not None:
        raise ValidationFailedError("this test session has ended; start a new one")
    if tool not in TOOL_NAMES:
        raise ValidationFailedError(f"unknown tool {tool!r}")
    executor = _executor(
        llm=llm,
        embeddings=embeddings,
        calendar=calendar,
        redis=redis,
        deadline_seconds=deadline_seconds,
    )
    return await _run_tool(
        executor, tenant=tenant, call=call, config=config, tool=tool, arguments=arguments
    )


async def send_message(
    *,
    tenant: Tenant,
    config: AgentConfig,
    call: Call,
    text: str,
    llm: LLMProvider,
    embeddings: Any,
    calendar: CalendarProvider | None,
    redis: Redis,
    deadline_seconds: float,
    now: datetime | None = None,
) -> TurnResult:
    """One caller turn: the model may call tools (each executed for real and
    traced) before it answers. Bounded at MAX_TOOL_ROUNDS so a model stuck in a
    tool loop costs a few calls, not an unbounded number."""
    if call.ended_at is not None:
        raise ValidationFailedError("this test session has ended; start a new one")
    text = text.strip()
    if not text:
        raise ValidationFailedError("say something to the receptionist")

    raw = await redis.get(_history_key(call.id))
    history: list[dict[str, Any]] = json.loads(raw) if raw else []
    history.append({"role": "user", "content": text})

    system = lab_system_prompt(
        tenant, config, now=now or datetime.now(UTC), greeting=config.first_message
    )
    tools = anthropic_tools(tool_schemas_for(tenant))
    executor = _executor(
        llm=llm,
        embeddings=embeddings,
        calendar=calendar,
        redis=redis,
        deadline_seconds=deadline_seconds,
    )

    turn = TurnResult(reply="")
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            completion = await llm.complete(
                CompletionRequest(
                    system=system, messages=list(history), max_tokens=400, tools=tools
                )
            )
        except Exception as exc:
            logger.warning("lab_llm_failed", error=type(exc).__name__)
            raise LabLLMUnavailableError(
                "the model could not be reached; check ANTHROPIC_API_KEY "
                "(the tool bench works without it)"
            ) from exc
        turn.model = completion.model
        turn.input_tokens += completion.input_tokens
        turn.output_tokens += completion.output_tokens

        if not completion.tool_calls:
            turn.reply = completion.text.strip()
            history.append({"role": "assistant", "content": turn.reply or "..."})
            break

        blocks: list[dict[str, Any]] = []
        if completion.text.strip():
            blocks.append({"type": "text", "text": completion.text})
        blocks.extend(
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("input") or {}}
            for c in completion.tool_calls
        )
        history.append({"role": "assistant", "content": blocks})

        results = []
        for tool_call in completion.tool_calls:
            event = await _run_tool(
                executor,
                tenant=tenant,
                call=call,
                config=config,
                tool=tool_call["name"],
                arguments=dict(tool_call.get("input") or {}),
            )
            turn.tool_events.append(event)
            results.append(
                {"type": "tool_result", "tool_use_id": tool_call["id"], "content": event.result}
            )
        history.append({"role": "user", "content": results})
    else:
        turn.reply = "Sorry, let me take your details and have someone call you back."
        history.append({"role": "assistant", "content": turn.reply})

    await redis.set(_history_key(call.id), json.dumps(history), ex=HISTORY_TTL_SECONDS)
    await _append_turns(
        tenant.id,
        call.id,
        [
            {"role": "caller", "text": text, "ts": _now_iso()},
            {"role": "assistant", "text": turn.reply, "ts": _now_iso()},
        ],
    )
    return turn


async def end_session(tenant_id: uuid.UUID, call: Call, *, redis: Redis) -> None:
    """Hang up: stamp the end time and hand the call to the post-call worker,
    so the summary and outcome can be checked too."""
    from app.core.queue import POST_CALL_QUEUE, enqueue

    async with tenant_session(tenant_id) as session:
        await session.execute(
            update(Call)
            .where(Call.id == call.id, Call.ended_at.is_(None))
            .values(ended_at=datetime.now(UTC))
        )
    await enqueue(
        redis,
        POST_CALL_QUEUE,
        {"type": "post_call", "tenant_id": str(tenant_id), "call_id": str(call.id)},
    )
    await redis.delete(_history_key(call.id))


async def discard_session(
    tenant_id: uuid.UUID, call: Call, *, calendar: CalendarProvider | None, redis: Redis
) -> int:
    """Remove everything a test session created. Bookings are cancelled through
    the calendar first, so a connected Google Calendar is cleaned up too.
    Returns how many appointments were removed."""
    async with tenant_session(tenant_id) as session:
        appointment_ids = list(
            (
                await session.execute(
                    select(Appointment.id).where(
                        Appointment.tenant_id == tenant_id, Appointment.call_id == call.id
                    )
                )
            ).scalars()
        )
    for appointment_id in appointment_ids:
        if calendar is not None:
            try:
                await cancel_appointment(
                    tenant_id=tenant_id, appointment_id=appointment_id, calendar=calendar
                )
            except Exception:
                logger.exception("lab_discard_calendar_cancel_failed")
    async with tenant_session(tenant_id) as session:
        await session.execute(delete(Appointment).where(Appointment.call_id == call.id))
        await session.execute(delete(CallbackRequest).where(CallbackRequest.call_id == call.id))
        await session.execute(delete(Lead).where(Lead.call_id == call.id))
        await session.execute(delete(Call).where(Call.id == call.id))  # cascades events, transcript
    await redis.delete(_history_key(call.id))
    return len(appointment_ids)


async def _append_turns(
    tenant_id: uuid.UUID, call_id: uuid.UUID, turns: list[dict[str, Any]]
) -> None:
    async with tenant_session(tenant_id) as session:
        stmt = pg_insert(Transcript).values(tenant_id=tenant_id, call_id=call_id, turns=turns)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["call_id"],
                set_={"turns": Transcript.turns.op("||")(stmt.excluded.turns)},
            )
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── board + activity ───────────────────────────────────────────────────


@dataclass
class BoardAppointment:
    id: uuid.UUID
    bay: int
    starts_at: datetime
    ends_at: datetime
    service: str
    customer_name: str
    details: dict[str, Any]
    simulated: bool


@dataclass
class Board:
    day: date
    timezone: str
    bays: int
    opening_hours: list[list[str]]
    appointments: list[BoardAppointment]


async def day_board(tenant: Tenant, day: date) -> Board:
    """One local day of confirmed appointments, by bay — the view a service
    writer keeps on the wall, and the quickest way to see capacity working."""
    tz = ZoneInfo(tenant.timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    end = start + timedelta(days=1)
    async with tenant_session(tenant.id) as session:
        rows = (
            await session.execute(
                select(Appointment, Call.direction)
                .outerjoin(Call, Call.id == Appointment.call_id)
                .where(
                    Appointment.tenant_id == tenant.id,
                    Appointment.status == "confirmed",
                    Appointment.ends_at > start,
                    Appointment.starts_at < end,
                )
                .order_by(Appointment.starts_at, Appointment.bay)
            )
        ).all()
    return Board(
        day=day,
        timezone=tenant.timezone,
        bays=bay_count(tenant),
        opening_hours=(tenant.business_hours or {}).get(WEEKDAY_KEYS[day.weekday()], []),
        appointments=[
            BoardAppointment(
                id=a.id,
                bay=a.bay,
                starts_at=a.starts_at,
                ends_at=a.ends_at,
                service=a.service,
                customer_name=a.customer_name,
                details=a.details or {},
                simulated=direction == SIMULATED,
            )
            for a, direction in rows
        ],
    )


@dataclass
class ActivityItem:
    id: uuid.UUID
    occurred_at: datetime
    event_type: str
    call_id: uuid.UUID
    caller_e164: str
    simulated: bool
    tool: str | None
    summary: str
    latency_ms: int | None


async def recent_activity(
    tenant_id: uuid.UUID, *, after: datetime | None = None, limit: int = 40
) -> list[ActivityItem]:
    """Newest-first stream of call events (real and simulated) for the live
    feed. `after` lets the dashboard poll for just what's new."""
    async with tenant_session(tenant_id) as session:
        stmt = (
            select(CallEvent, Call.caller_e164, Call.direction)
            .join(Call, Call.id == CallEvent.call_id)
            .where(CallEvent.tenant_id == tenant_id)
            .order_by(CallEvent.occurred_at.desc())
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(CallEvent.occurred_at > after)
        rows = (await session.execute(stmt)).all()
    items = []
    for event, caller, direction in rows:
        payload = event.payload or {}
        tool = payload.get("tool")
        result = str(payload.get("result", ""))
        items.append(
            ActivityItem(
                id=event.id,
                occurred_at=event.occurred_at,
                event_type=event.event_type,
                call_id=event.call_id,
                caller_e164=caller,
                simulated=direction == SIMULATED,
                tool=tool,
                summary=(result[:140] + "…") if len(result) > 140 else result,
                latency_ms=payload.get("latency_ms"),
            )
        )
    return items
