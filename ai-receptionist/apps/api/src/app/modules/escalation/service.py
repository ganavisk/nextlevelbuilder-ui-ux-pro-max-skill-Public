"""Escalation: the v1 voicemail-plus-callback path.

There is no live transfer in v1 (Phase 1 decision) — every escalation
trigger (caller asks for a human, out-of-scope question, distress) routes
here, becoming a row the dashboard's callback queue reads (Phase 7).
"""

import re
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.db import table_of, tenant_session
from app.core.errors import NotFoundError, ValidationFailedError
from app.modules.escalation.models import CallbackRequest
from app.modules.scheduling.availability import (
    next_opening,
    next_reopening,
    spoken_day,
    spoken_time,
)

# open -> contacted -> resolved (migration 0001's CHECK constraint). Not
# stored as a state machine anywhere else, so this is the one place that
# knows the full set — the dashboard PATCH validates against it directly
# rather than relying on the database CHECK to be the only guard (a 500 from
# a constraint violation is a worse UX than a 422 that names the problem).
VALID_STATUSES = ("open", "contacted", "resolved")


DEFAULT_PROMISE = "Someone from the team will call you back."


def callback_promise_for(
    escalation_policy: dict[str, Any],
    business_hours: dict[str, list[list[str]]],
    timezone: str,
    now: datetime,
) -> str:
    """What to tell a caller about when they'll hear back.

    `callback_promise` ("within 30 minutes during shop hours") is only true
    while the business is open. If the tenant also configures
    `after_hours_promise`, that is used when it's closed, with `{next_open}`
    filled in as spoken ("tomorrow at 7:30 am"), so a caller at 9 pm isn't
    left waiting for a call that won't come until morning.
    """
    promise = str(escalation_policy.get("callback_promise") or DEFAULT_PROMISE)
    after_hours = escalation_policy.get("after_hours_promise")
    if not after_hours:
        return promise
    opens = next_opening(business_hours or {}, timezone, now)
    if opens is None:
        return promise
    return str(after_hours).replace(NEXT_OPEN, _spoken(opens, timezone, now))


def _spoken(opens: datetime, timezone: str, now: datetime) -> str:
    today = now.astimezone(ZoneInfo(timezone)).date()
    return f"{spoken_day(opens.date(), today)} at {spoken_time(opens)}"


def example_next_open(
    business_hours: dict[str, list[list[str]]], timezone: str, now: datetime
) -> str | None:
    """What {next_open} would say for a caller who rang after today's
    closing, for the Settings preview. None without business hours."""
    opens = next_reopening(business_hours or {}, timezone, now)
    return _spoken(opens, timezone, now) if opens is not None else None


MAX_TRIGGERS = 10
MAX_TRIGGER_LENGTH = 160


def validate_triggers(raw: object) -> list[str]:
    """The situations a business wants handed to the team, as typed in
    Settings: whitespace tidied, blanks and repeats dropped (keeping the
    first spelling), each short enough to be one clear instruction."""
    if not isinstance(raw, list):
        raise ValidationFailedError("Triggers must be a list of situations.")
    triggers: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").split())
        if not text or text.lower() in seen:
            continue
        if len(text) > MAX_TRIGGER_LENGTH:
            raise ValidationFailedError(
                f"Keep each situation under {MAX_TRIGGER_LENGTH} characters: "
                f'"{text[:40]}..." is too long.'
            )
        seen.add(text.lower())
        triggers.append(text)
    if len(triggers) > MAX_TRIGGERS:
        raise ValidationFailedError(
            f"Up to {MAX_TRIGGERS} situations. A long list makes the receptionist "
            "hand over calls it could handle."
        )
    return triggers


NEXT_OPEN = "{next_open}"
MAX_PROMISE_LENGTH = 300
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def validate_promises(callback_promise: object, after_hours_promise: object) -> dict[str, str]:
    """Check the two promise texts a dashboard user typed; return the policy
    keys to merge (an empty after-hours text means "remove it").

    Both are read aloud as written, so a mistyped placeholder ("{next_opne}")
    would reach callers word for word: refuse anything in braces but
    {next_open}, and {next_open} itself in the open-hours promise, where it
    is never filled in."""
    promise = " ".join(str(callback_promise or "").split())
    after = " ".join(str(after_hours_promise or "").split())
    if not promise:
        raise ValidationFailedError("The callback promise can't be empty.")
    for label, text in (("callback promise", promise), ("after-hours promise", after)):
        if len(text) > MAX_PROMISE_LENGTH:
            raise ValidationFailedError(
                f"The {label} is too long to say on a call (max {MAX_PROMISE_LENGTH} characters)."
            )
    if NEXT_OPEN in promise:
        raise ValidationFailedError(
            "{next_open} only works in the after-hours promise; the open-hours one is "
            "said while you're open."
        )
    unknown = _PLACEHOLDER.findall(promise) + [
        p for p in _PLACEHOLDER.findall(after) if p != NEXT_OPEN
    ]
    if unknown:
        raise ValidationFailedError(
            f"{unknown[0]} isn't something the receptionist can fill in. Use {NEXT_OPEN} "
            "for when you next open."
        )
    return {"callback_promise": promise, "after_hours_promise": after}


MAX_NAME_LENGTH = 120


def clean_name(raw: object) -> str:
    """A name as the model wrote it, made safe to store and show: whitespace
    collapsed, length capped. Not otherwise judged: names don't follow rules."""
    name = " ".join(str(raw or "").split())
    return name[:MAX_NAME_LENGTH]


async def create_callback_request(
    *,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID | None,
    caller_e164: str,
    reason: str,
    preferred_window: str | None = None,
    voicemail_transcript: str | None = None,
    callback_e164: str | None = None,
    caller_name: str = "",
) -> uuid.UUID:
    """`callback_e164` is a number the caller asked to be called back on
    instead of the one they called from (already normalized). `caller_name`
    is who to ask for, as the agent heard it."""
    async with tenant_session(tenant_id) as session:
        request = CallbackRequest(
            tenant_id=tenant_id,
            call_id=call_id,
            caller_e164=caller_e164,
            callback_e164=callback_e164,
            caller_name=clean_name(caller_name),
            reason=reason,
            preferred_window=preferred_window,
            voicemail_transcript=voicemail_transcript,
        )
        session.add(request)
        await session.flush()
        return request.id


async def update_callback_status(
    tenant_id: uuid.UUID, request_id: uuid.UUID, status: str
) -> CallbackRequest:
    if status not in VALID_STATUSES:
        raise ValidationFailedError(f"invalid status {status!r}, must be one of {VALID_STATUSES}")
    async with tenant_session(tenant_id) as session:
        await session.execute(
            table_of(CallbackRequest)
            .update()
            .where(CallbackRequest.id == request_id, CallbackRequest.tenant_id == tenant_id)
            .values(status=status)
        )
        request = (
            await session.execute(select(CallbackRequest).where(CallbackRequest.id == request_id))
        ).scalar_one_or_none()
        if request is None:
            raise NotFoundError(f"no callback request {request_id}")
        session.expunge(request)
        return request
