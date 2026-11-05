# telephony

**Purpose:** Voice-vendor boundary and call lifecycle. Verifies inbound
webhooks, normalizes vendor events, persists calls/transcripts/events, keeps
per-call Redis state, and hands finished calls to the post-call queue.

**Dependencies:** `core` (db, queue, security, config), `tenants`
(number → tenant resolution). No other module imports.

**Inputs:** Vapi webhooks at `POST /webhooks/voice/vapi` (secret-header
authenticated). **Outputs:** rows in `calls` / `transcripts` / `call_events`,
Redis call-session keys (`call:<vendor_call_id>`, TTL-bounded), jobs on
`queue:post_call`.

**Configuration:** `VOICE_PROVIDER`, `VAPI_API_KEY`, `VAPI_WEBHOOK_SECRET`,
`MAX_CALL_DURATION_SECONDS`.

**Design rules:**
- Only `providers/vapi.py` knows Vapi's wire format; everything else consumes
  `NormalizedCallEvent`.
- The webhook path does no LLM work and no integration I/O (hot-path latency
  budget from Phase 1 §3.3).
- All handlers are idempotent — vendors retry webhooks.
- Vendors don't guarantee webhook order or delivery either: the first event
  for a call we have no record of (a lost or late call-started) registers the
  call (`_register_call`, logged as `call_registered_late`) instead of
  failing every tool call on it. Found by the load test (`docs/load-test.md`).
- The agent is told the business's current local time on every call. The
  provisioned prompt is static, so `providers/vapi.py` appends
  `clock_instructions` with Vapi's per-call LiquidJS template
  (`{{"now" | date: "...", "<tenant tz>"}}`; Vapi's `now` is UTC). Without it
  the model guesses what "tomorrow" is. A new adapter must do the same in its
  own template syntax.

**Future improvements:** Retell adapter; tool-call dispatch to the tool
executor (Phase 4); in-call transcript streaming persistence.

**Known risks:** Vapi payload shapes are covered by fixtures but not yet
contract-validated against the live sandbox (waits on API keys — M1 live
test); drift is confined to `providers/vapi.py`. That includes the date
template: the tests check what we send, but only a live call proves Vapi
renders it.
- The timezone is baked into the template when the agent is pushed. A
  Settings change re-pushes it automatically (`tenants/voice_sync.py`); a
  change made outside the dashboard (a YAML re-seed, a code change) still
  needs `make provision SLUG=...`.
