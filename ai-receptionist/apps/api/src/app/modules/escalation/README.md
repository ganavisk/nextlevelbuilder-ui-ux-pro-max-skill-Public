# escalation

**Purpose:** The v1 escalation path — voicemail-plus-callback. Every
escalation trigger (caller asks for a human, out-of-scope question, distress)
becomes a `callback_requests` row for the dashboard's callback queue. There
is no live transfer in v1 (Phase 1 product decision).

**Dependencies:** `core` (db). Called from `conversation.tools.ToolExecutor`
(the `request_callback` tool) and, later, the post-call worker for missed
calls.

**Inputs:** caller, reason, optional caller name, preferred window, voicemail
transcript and callback number.
**Outputs:** a `callback_requests` row (open → contacted → resolved).

**Which number to call:** `caller_e164` is the caller ID and is always kept.
If the caller asks to be called back elsewhere, the `request_callback` tool's
`customer_phone` is normalized (`core/phone.py`) into `callback_e164`, which
the dashboard dials first, showing "Called from ..." underneath. A misheard
number saves nothing and is asked for again, and the new number is read back
as single digits. A blocked caller ID with no number given is asked for one,
so the queue never holds a promise nobody can keep.

**Who to ask for:** the tool's `customer_name` goes into `caller_name`
(whitespace tidied, capped at 120 characters), kept apart from the reason so
the queue can lead with it. It is optional on purpose: a caller who won't
give a name still gets their callback.

**Configuration:** none beyond `core`; escalation triggers and the callback
promise live in the tenant's `agent_configs.escalation_policy`.

**Future improvements:** SLA timers / overdue-callback alerts; optional live
transfer as a v2 escalation mode behind the same trigger set.

**Known risks:** none specific at this stage — the module is a thin, typed
write path over one table.
