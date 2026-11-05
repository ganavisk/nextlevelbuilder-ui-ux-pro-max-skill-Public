# AI Receptionist Platform — Phase 6: CRM Sync

**Status:** Delivered — awaiting product-owner approval before Phase 7 (Reporting)
**Milestone:** M4 "CRM Sync" — code-complete and integration-tested against live Postgres +
Redis. Live-HubSpot validation still waits on HubSpot OAuth credentials.

---

## 1. What Phase 6 delivers

A finished call pushes its lead into the tenant's HubSpot: a contact, a logged call activity,
and — when the call earned one — a deal. The job pipeline underneath it now has real retry,
backoff, and dead-lettering, proven by fault injection rather than asserted by inspection.

| Component | Where | Verified |
|---|---|---|
| **Reliable job queue**: retry, backoff+jitter, dead-letter, crash recovery | `core/queue.py` | ✅ 13 fault-injection integration |
| **CRM sync service** — resumable, race-safe | `crm/service.py` | ✅ 11 integration (real Postgres) |
| **HubSpot adapter** behind `CRMProvider` | `crm/providers/hubspot.py` | ✅ 17 contract (mock transport) |
| **Per-tenant field mapping** | `crm/mapping.py` | ✅ 18 unit |
| **CRM worker** | `workers/crm_sync.py` | ✅ exercised via service + queue tests |
| Post-call → CRM handoff | `workers/post_call.py` | ✅ enqueues after classification |
| `leads.crm_synced_at` mapped (was missing from the ORM) | `conversation/models.py` | ✅ |
| Migration 0006 (crm_syncs ledger, RLS) | `migrations/versions/0006_*.py` | ✅ applied, indexes verified |

**Test count: 182 (up from 123 at Phase 5 close).** No live HubSpot credentials needed: the
adapter is driven against `httpx.MockTransport`; everything else runs against real Postgres +
Redis with a fake `CRMProvider`.

## 2. How a call reaches the CRM

```
Post-call worker classifies the call (outcome, summary)
   └─▶ enqueue queue:crm  {tenant_id, call_id, outcome, summary, duration_seconds}
        └─▶ CRM worker: sync_call(tenant, call_id, crm, ...)
             ├─ claim the ledger row (atomic status transition — see §3)
             ├─ 1. upsert_contact   (skip if contact_external_id already set)
             ├─ 2. log_activity     (skip if activity_external_id already set)
             ├─ 3. create_deal      (skip if deal_external_id set, or the call
             │                        didn't earn one — mapping.should_create_deal)
             └─ status = 'synced'; leads.crm_contact_id / crm_synced_at stamped
```

A call with no matching lead (wrong number, hang-up) is skipped, not failed — retrying it
would spin forever for a call that will never have one.

## 3. Two guarantees, and why both were needed

**Resumability.** No HubSpot write is idempotent from our side — there is no idempotency
key to hand it, and `create_deal` has no natural key at all. Simply retrying a failed job
from the top would repeat whatever already succeeded: a second activity on the timeline, or
a second deal. So each external id is recorded in `crm_syncs` the instant it's obtained, and
a retry skips any step that already has one.

**Concurrency safety.** Converging two workers on the same ledger row (via a unique
idempotency key) turned out not to be sufficient by itself. `core/queue.py`'s delivery
guarantee is at-least-once: a worker presumed dead can be reclaimed while genuinely still
running, and the reclaimed job then executes *concurrently* with its still-alive original.
Both would load the same `NULL` columns and both write — the first version of this code did
exactly that, and a concurrency test caught it (§5). The fix is `_claim_attempt`: an atomic
UPDATE that only one concurrent caller can win, transitioning the row `pending`/`failed` (or
a stale `in_progress`) → `in_progress`. The loser performs zero steps, rather than racing
step-by-step and hoping the writes land in a safe order.

## 4. The job queue is now a real reliability layer

Phase 6 rewrote `core/queue.py` from RPUSH/BLPOP into something with the properties M4 was
scoped to prove:

- **At-least-once delivery** via `BLMOVE ready → processing`, so a worker killed mid-job
  leaves the payload recoverable rather than losing it. `reclaim_orphans` returns stranded
  jobs to the ready list on worker startup.
- **Exponential backoff with full jitter.** Jitter matters because a vendor outage fails
  every in-flight job at once; without it, backoff retries them all in the same instant and
  knocks the vendor over again the moment it recovers.
- **Two failure classes.** `IntegrationError` (5xx, timeouts, rate limits) retries up to
  `max_attempts` then dead-letters. `PermanentIntegrationError` (400/401/403/404/422 —
  malformed payload, revoked grant, deleted object) dead-letters immediately: retrying a 400
  five times only delays the alert.
- **A bounded Redis dead-letter list** as a diagnostic buffer — durable failure state lives
  in the database (`crm_syncs.status = 'dead'`), not in Redis alone, so it's visible to
  anything outside the worker process.

This queue now backs all three async-path workers (post-call, confirmations, CRM), so Phase
5's confirmations gained retry/backoff/dead-lettering as a side effect of this phase's work.

## 5. Verification performed (this session)

- **182 tests pass**; `ruff check` and `ruff format --check` clean.
- Migration 0006 applied against live Postgres; `crm_syncs` RLS (enabled *and* forced) and
  its two indexes confirmed via `\d`.
- **Fault injection, not simulation:** `tests/test_queue_retry.py` runs real Redis, injects
  real failures, and asserts on real Redis state — a transient failure that succeeds on the
  third attempt, an exhausted retry that dead-letters with the reason and the original
  payload intact, a permanent failure that skips retries entirely, and a worker killed
  mid-job (`task.cancel()` while a handler is suspended) whose job is provably recoverable
  by the next `reclaim_orphans` call.
- **The concurrency test found a real bug twice** (§6): once in the queue's own cancellation
  handling, once in the CRM service's step-level races. Both are fixed and now covered.
- HubSpot adapter contract-tested: contact create vs. 409-then-search-and-update vs.
  phone-match-first-when-no-email; activity and deal request shape and association ids;
  429/5xx classified transient, 400/401/403/404/422 classified permanent; token refresh,
  refresh persistence, and revoked-grant marking (same pattern as the Phase 5 Google adapter).

## 6. Defects found and fixed this phase

**A `finally` block defeated crash recovery — caught by the crash-recovery test itself.**
The first version of `run_worker` removed a job from `processing` in a `finally` clause that
ran even on `asyncio.CancelledError`. Since cancellation is exactly what a killed worker
looks like, this quietly deleted a job's only recoverable copy at the moment it was needed
most — the one scenario reclaim exists for. Restructured so cancellation propagates before
any cleanup runs; only a completed or definitively-failed job is removed from `processing`.

**CRM sync had a step-level race under concurrent execution.** Loading ledger state once and
checking each column for `None` is correct for sequential retries but not for two attempts
running at the same instant — both see the same `None` and both write. Root-caused to
`core/queue.py`'s own at-least-once/reclaim design (§3) rather than dismissed as
unlikely — a reclaimed job racing its "zombie" original is a documented, expected shape of
failure. Fixed with an atomic per-attempt claim (`_claim_attempt`) rather than a per-step
claim, since a lost race after step 1 has already run would leave nothing to resume.

**`leads.crm_synced_at` existed in the database (migration 0001) but was never mapped on the
ORM model.** Nothing before this phase read or wrote it, so the gap was invisible until CRM
sync needed the column. Added to `conversation/models.py`.

## 7. Design notes for the reviewer

- **The worker decides "no", not the sync service.** `mapping.should_create_deal` is a pure
  function the worker can log and test independently of any vendor call — a wrong deal rule
  is a one-line config or logic fix, never a partially-executed sync to unwind.
- **Contact matching is explicit, not assumed.** HubSpot dedupes on email only. Since most
  callers give a phone number and no email, `upsert_contact` searches by phone itself before
  creating — skipping that step is the single most common way to silently double a CRM's
  contact list.
- **A `failed` sync is not `dead`.** `failed` means "this attempt didn't finish, and the
  queue may still retry it." `dead` means "the queue gave up." Both are visible in
  `crm_syncs`, but only `dead` means a human needs to look.
- **A tenant can turn CRM sync off entirely** (`crm.enabled: false`) without touching code —
  the worker checks this before doing anything, including claiming a ledger row.

## 8. What M4 still needs from you (for the live demo)

| Need | Why | Where it goes |
|---|---|---|
| HubSpot OAuth client id + secret | app-level HubSpot connection | `HUBSPOT_CLIENT_ID` / `_SECRET` |
| A HubSpot account to connect | the practice's actual CRM | `integrations` row, via the connect flow (Phase 7 UI; seedable by hand until then) |

Until these exist, CRM sync works end-to-end against the database and is proven by the test
suite; only the HubSpot round-trip is unexercised.

## 9. Known gaps carried into Phase 7

- HubSpot is contract-tested, not live-tested. Locked down against documented behaviour, not
  yet proven against the real API.
- No dashboard view over `failed_syncs()` yet — the data exists, nothing renders it. This is
  explicitly Phase 7 scope.
- `CLAIM_STALE_AFTER` (5 minutes) is a judgment call with no operational data behind it yet.
- No two-way sync: a deal stage a human changes in HubSpot is never read back into our data
  model, so the dashboard's view of a lead's status can drift from HubSpot's.
- Deal amount/close-date are not populated — there's no pricing model yet to derive them from.

## 10. Next: Phase 7 — Reporting (on your approval)

A Next.js dashboard: call history, lead pipeline (including the `failed_syncs()` queue this
phase built for it), analytics rollups, and the tenant settings UI — including the
Google/HubSpot connect flows that have, until now, required seeding `integrations` rows by
hand.
