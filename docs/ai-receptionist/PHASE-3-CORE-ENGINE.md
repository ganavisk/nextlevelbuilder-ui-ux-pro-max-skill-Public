# AI Receptionist Platform — Phase 3: Core Engine

**Status:** Delivered — awaiting product-owner approval before Phase 4 (Conversation Engine)
**Milestone:** M1 "First Call" — code-complete and integration-tested; the live-call demo
waits only on Vapi + Twilio credentials.

---

## 1. What Phase 3 delivers

The vendor-agnostic call engine: a phone call now flows from webhook to database.

| Component | Where | Verified |
|---|---|---|
| **Vapi adapter** (`VoiceProvider` impl): secret verification, event normalization, agent sync, number attach, tool-result formatting | `telephony/providers/vapi.py` | ✅ 8 fixture tests |
| Provider factory (adapter selection by config) | `telephony/providers/__init__.py` | ✅ |
| **Webhook ingress**: verify → normalize → dispatch | `telephony/routes.py` (`POST /webhooks/voice/vapi`) | ✅ incl. 401 on unsigned |
| **Call lifecycle service**: tenant resolution, idempotent call creation, transcript + event persistence, Redis call state, queue handoff | `telephony/service.py` | ✅ end-to-end |
| **Tenant resolution by inbound number** + RLS policy for pre-identity lookup | `tenants/repository.py`, migration `0002` | ✅ |
| Agent-definition assembly (persona + forbidden-topics guardrail + voicemail-callback script) | `tenants/service.py` | ✅ |
| ORM models (tenants, phone numbers, agent configs, calls, transcripts, call events) | `tenants/models.py`, `telephony/models.py` | ✅ |
| **Redis job queue** + post-call worker skeleton (`make worker`) | `core/queue.py`, `workers/post_call.py` | ✅ live run |
| Demo-tenant seed script from YAML (`make seed`) | `scripts/seed_demo.py` | ✅ live run |
| Module documentation (purpose/deps/IO/config/risks) | `telephony/README.md`, `tenants/README.md` | — |

## 2. The call lifecycle as implemented

```
Vapi webhook ──▶ verify x-vapi-secret (constant-time; 401 otherwise)
            ──▶ adapter normalizes to NormalizedCallEvent
CALL_STARTED ─▶ resolve tenant by dialed number ─▶ idempotent INSERT calls
                ─▶ Redis state call:<vendor_id> (TTL = max duration + 10 min)
TOOL_CALL /  ─▶ call_events row (tool dispatch itself is Phase 4)
STATUS/TRANSCRIPT
CALL_ENDED  ──▶ ended_at, recording URL, vendor cost (cents) on the call row
                ─▶ transcript persisted (system/tool frames filtered out)
                ─▶ job on queue:post_call ─▶ worker finalizes (idempotent)
                ─▶ Redis state deleted
```

Reliability properties, by construction:
- **Idempotent everywhere** — vendors retry webhooks; call creation upserts on
  `(tenant_id, vendor_call_id)`, worker updates only unclassified calls.
- **Redis loss is survivable** — `_resolve_call` falls back to DB resolution, so a
  Redis flush mid-call cannot drop an end-of-call report.
- **Hot path stays hot** — the webhook path does zero LLM and zero integration I/O.

## 3. Verification performed (this session)

1. **18/18 tests pass** (9 Phase-2 + 8 adapter fixture tests + 1 full integration test).
2. The integration test drives the real HTTP endpoint against live Postgres + Redis:
   unsigned request → 401; started/tool-call/ended fixtures → call row with `ended_at`,
   recording URL, and `vendor_cost_cents=37`; transcript with correctly role-mapped turns
   (system frames excluded); `tool_call` + `call_ended` events; exactly one post-call job.
3. **Both migrations applied to a fresh database** from zero (`FRESH_MIGRATION_OK`).
4. **RLS re-verified** after the policy fix (see §4): cross-tenant reads return 0 rows,
   forged inserts still rejected.
5. **Seed script** created the Bright Smile Dental demo tenant from its YAML;
   **worker** consumed a real queued job and finalized the call's outcome.
6. Lint clean (`ruff check` + format).

## 4. Defect found and fixed

The Phase-2 RLS policies read `current_setting('app.tenant_id')` without `missing_ok`,
which *errors* (rather than filtering) in sessions that never set the variable — exposed
the first time an unscoped code path touched an RLS table. Fixed in migration 0001
(greenfield edit) to `current_setting('app.tenant_id', true)`: unset now yields NULL,
meaning "no rows visible", which is the correct fail-closed behavior. Isolation
semantics re-verified (§3.4). Companion migration 0002 adds the one deliberate
exception: a SELECT-only policy on `phone_numbers` so the number → tenant lookup can
run before tenant identity exists.

## 5. Design notes for the reviewer

- **Vendor knowledge is quarantined.** Only `providers/vapi.py` knows Vapi's payload
  shapes (message envelope, `x-vapi-secret` shared-secret auth, `end-of-call-report`
  artifacts). Fixtures encode that contract; the M1 live test validates it against the
  real sandbox, and any drift is a one-file fix.
- **Prompt assembly is platform-owned** (`tenants/service.py`), never delegated to the
  vendor: the runtime prompt = persona + forbidden-topics block (Risk R3 guardrail) +
  voicemail-callback script with the tenant's callback promise (your v1 escalation
  decision).
- **Queue is deliberately minimal** (Redis RPUSH/BLPOP, idempotent consumers). Retry
  with backoff and dead-lettering land in Phase 4 with the first consumers that talk to
  external systems; `arq` is the designated upgrade if needed, behind the same two
  functions.

## 6. What M1 still needs from you (for the live demo)

1. **Vapi account** → API key + a server-URL secret (goes in `VAPI_API_KEY` /
   `VAPI_WEBHOOK_SECRET`).
2. **Twilio account** → one phone number (imported into Vapi).
3. A publicly reachable deployment or tunnel for the webhook URL — Phase 8 sets up
   Railway properly; for the demo, any HTTPS tunnel to `/webhooks/voice/vapi` works.

With those in hand the sequence is: `make seed` → `sync_agent` + `attach_number` for the
demo tenant → call the number → the AI answers with the Bright Smile persona and the
call, transcript, and cost land in the database.

## 7. Next: Phase 4 — Conversation Engine (on your approval)

Milestone M2 "Smart Conversations": knowledge-base ingestion + pgvector RAG (Voyage
embeddings), FAQ answering grounded in tenant knowledge only, lead qualification flows
with scoring (from the YAML configs), voicemail-plus-callback capture into
`callback_requests`, the in-call tool executor (dispatching Vapi `tool-calls` with the
<2 s budget), and the real post-call pipeline (Haiku summary, sentiment, lead scoring).
This is the phase where the Claude API integration lands, plus the conversation eval
suite that gates releases.
