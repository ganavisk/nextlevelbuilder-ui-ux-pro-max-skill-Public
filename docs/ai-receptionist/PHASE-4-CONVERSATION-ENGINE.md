# AI Receptionist Platform — Phase 4: Conversation Engine

**Status:** Delivered — awaiting product-owner approval before Phase 5 (Scheduling)
**Milestone:** M2 "Smart Conversations" — code-complete and integration-tested against
live Postgres + Redis. Live-call validation still waits on Vapi/Voyage/Anthropic keys.

---

## 1. What Phase 4 delivers

The AI brain: the agent can now answer FAQs from a knowledge base, qualify leads, and
escalate — all grounded, all tenant-isolated.

| Component | Where | Verified |
|---|---|---|
| **LLM adapter** (Claude): Sonnet in-call, Haiku for classification; per-tier param handling | `conversation/providers/anthropic_llm.py` | ✅ via fakes |
| **Embedding adapter** (Voyage), query/document asymmetry | `conversation/providers/voyage.py` | (live-key deferred) |
| **Knowledge ingestion**: chunk + embed, idempotent re-index | `conversation/knowledge.py` | ✅ 4 unit + integration |
| **RAG retrieval**: pgvector cosine search + confidence floor | `conversation/retrieval.py` | ✅ integration (real pgvector) |
| **Grounded FAQ answering** with the hallucination guardrail | `conversation/faq.py` | ✅ integration |
| **Lead qualification** scoring + atomic upsert | `conversation/qualification.py` | ✅ 4 unit + 2 integration |
| **Post-call summarization**: outcome/sentiment/summary via Haiku | `conversation/summarization.py` | ✅ 5 unit + worker E2E |
| **LLM cost metering** (per-tenant, NFR-09) | `conversation/pricing.py` | ✅ 3 unit |
| **In-call tool executor** + tool schemas | `conversation/tools.py` | ✅ 2 unit + webhook integration |
| **Voicemail-callback capture** | `escalation/service.py` + `escalation/models.py` | ✅ webhook integration |
| Webhook TOOL_CALL dispatch wired end-to-end | `telephony/routes.py` | ✅ full HTTP integration |
| Real post-call pipeline in the worker | `workers/post_call.py` | ✅ E2E run |
| Knowledge-base seeding in demo tenants (dental + legal) | `scripts/seed_demo.py`, `config/tenants/*.yaml` | ✅ |
| Migration 0003 (leads per-call unique index) | `migrations/versions/0003_*.py` | ✅ up + down |

**Test count: 43 (up from 18).** Unit tests run without services; integration tests use
fake LLM/embedding providers against real Postgres + Redis, so no live API keys are
needed to prove the database, RAG, and webhook wiring.

## 2. How a smart call now flows

```
Caller question ─▶ Vapi (model + tools from tenant config) decides to call answer_faq
             ─▶ TOOL_CALL webhook ─▶ route resolves (tenant, call) via Redis/DB
             ─▶ ToolExecutor.dispatch:
                  answer_faq         ─▶ embed question ─▶ pgvector search (tenant-scoped)
                                       ─▶ chunks below distance floor? ground answer via Sonnet
                                       ─▶ else safe fallback (no LLM call at all)
                  record_qualification_answer ─▶ deterministic score ─▶ atomic lead upsert
                  request_callback   ─▶ callback_requests row ─▶ tenant's callback promise
             ─▶ adapter formats results ─▶ spoken back to caller
Call ends ─▶ post-call worker: Haiku classifies outcome/sentiment + one-line summary,
             meters cost ─▶ writes to the call row (all idempotent)
```

## 3. The hallucination guardrail (Risk R3), concretely

FAQ answering is grounded-only, enforced at three points:
1. **Retrieval floor** — chunks beyond a cosine-distance threshold are discarded; if none
   remain, the tool returns the safe "I'll have someone follow up" fallback **without
   calling the LLM at all** (proven by `test_webhook_tool_call`: `fake_llm.calls == []`).
2. **Prompt contract** — the LLM is instructed to answer only from the provided context
   and emit the exact fallback string otherwise.
3. **Failure degradation** — an empty/refused completion or any exception also routes to
   the fallback. The agent never guesses.

Forbidden-topic guardrails (no clinical advice / no legal advice) are injected into the
tenant system prompt and steer the model to `request_callback` instead.

## 4. Verification performed (this session)

1. **43/43 tests pass.** Real pgvector RAG retrieval (relevant chunk ranks first; a
   near-identical *other-tenant* chunk is excluded by RLS, not just ranking); qualification
   upsert merges JSONB answers and accumulates score, and converges on one lead row even
   under `asyncio.gather` concurrent writes; the full TOOL_CALL webhook path (answer_faq +
   request_callback) through real HTTP with fakes swapped in.
2. **Post-call worker run end-to-end**: seeded a call + transcript, enqueued the job, ran
   the worker with a fake Haiku — it classified the call (`answered_faq`, positive) and
   wrote the summary back under a tenant-scoped session.
3. **Migrations**: applied 0001→0003 to a fresh database and fully reversed to base.
4. Lint + format clean.

## 5. Defects found and fixed this phase

- **RLS pooled-connection edge case (production bug).** Once `SET LOCAL app.tenant_id`
  has run on a physical connection, Postgres can leave `current_setting()` returning `''`
  (not NULL) when that pooled connection is later reused for an unscoped session — which
  crashed the `::uuid` cast and, worse, could have silently broken the number-lookup RLS
  policy. Fixed by wrapping every policy's read in `NULLIF(current_setting(...), '')` and
  having `admin_session()` clear the variable explicitly. Isolation re-verified.
- **Partial-index upsert.** The leads `ON CONFLICT` targets a *partial* unique index
  (`WHERE call_id IS NOT NULL`); Postgres requires the predicate repeated via
  `index_where`, added.

Both were caught by the new integration tests — exactly what they're for.

## 6. Design notes for the reviewer

- **Providers are swapped once at startup** (`main.py` lifespan), reused across requests,
  and overridable on `app.state` for tests — no live keys needed to exercise the real DB
  and routing. The Claude/Voyage adapters stay the only files that know those SDKs.
- **Model tiers are deliberate:** in-call answering disables thinking and uses low effort
  (latency budget); post-call summarization uses Haiku and omits the thinking/effort
  params entirely (Haiku rejects them). Cost is metered per call from real token usage.
- **Qualification is LLM-free** — deterministic scoring against the YAML flow, so it's
  fast, cheap, and testable; Claude only conducts the conversation.

## 7. What M2 still needs from you (for the live demo)

Same accounts as M1 (Vapi + Twilio), plus:
- **Anthropic API key** (`ANTHROPIC_API_KEY`) — powers in-call FAQ answering + summaries.
- **Voyage API key** (`VOYAGE_API_KEY`) — powers knowledge-base embedding; `make seed`
  ingests the demo knowledge base automatically once it's set (and skips, with a warning,
  when it isn't).

Then: `make seed` → call the number → ask "do you take new patients?" and the agent
answers from the seeded knowledge base; ask for a human and a callback request appears in
the database; after hanging up, the call shows an outcome, sentiment, and summary.

## 8. Next: Phase 5 — Scheduling (on your approval)

Milestone M3: the Google Calendar adapter behind `CalendarProvider`, a `check_availability`
+ `book_appointment` tool pair (availability check on the hot path, booking with
idempotency keys and an atomic re-check to prevent double-bookings under concurrency), the
`appointments` write path, and email + SMS confirmations via the notifications module
(Resend + Twilio adapters). Demo: a caller books a real Google Calendar slot and gets an
email + SMS confirmation, with a concurrency test proving no double-booking.
