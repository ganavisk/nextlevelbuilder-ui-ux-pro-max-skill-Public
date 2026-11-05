# crm

**Purpose:** Push a finished call's lead into the tenant's CRM — contact,
call activity, and (when the call earned one) a deal — without ever leaving
a duplicate behind. Field mapping is per-tenant configuration; vendors sit
behind `CRMProvider` and are replaceable.

**Dependencies:** `core` (db, config, logging, queue, errors),
`tenants` (the `Tenant` row supplies `settings.crm`), `conversation` (reads
the `Lead` row a sync is for). Driven by `app.workers.crm_sync`, enqueued
from `app.workers.post_call` once a call is classified — never from the call
path, and never called directly by another module.

**Inputs:** a job `{type, tenant_id, call_id, outcome, summary,
duration_seconds}` from `queue:crm`.
**Outputs:** a HubSpot contact, call activity, and optional deal; a
`crm_syncs` row tracking progress; `leads.crm_contact_id` /
`leads.crm_synced_at` stamped on success.

## Why a sync is not "call three endpoints"

A sync is three writes against an API with no idempotency key: upsert the
contact, log the call, create the deal. If a failure partway through were
simply retried from the top, the steps that already succeeded would run
again — and `create_deal` has no natural key, so the practice would end up
with two deals for one phone call. Duplicates in someone's CRM are worse
than a missing record: they get noticed, and a human has to merge them.

So each external id is written to `crm_syncs` the instant it's obtained, and
a retry skips whatever already has one:

```
attempt 1:  contact ✓ (id saved)   activity ✓ (id saved)   deal ✗ ── raise
attempt 2:  contact skipped         activity skipped         deal ✓
```

## The concurrency guard

Converging two workers on the same ledger row (via a unique idempotency key)
is not by itself enough. The queue's delivery guarantee is at-least-once
(`core/queue.py`): a worker presumed dead can be reclaimed while genuinely
still running, and the reclaimed job then executes *concurrently* with its
still-alive original. Both would see the same `NULL` columns and both write.

`crm.service._claim_attempt` closes that gap with an atomic status
transition — only one concurrent caller can move the row from
`pending`/`failed` (or a stale `in_progress`) to `in_progress`. The loser
does no work at all, rather than racing step-by-step and hoping the DB
writes happen to land in a safe order. A claim older than
`CLAIM_STALE_AFTER` (5 minutes) is presumed abandoned and may be reclaimed —
well above the adapter's own 20s timeout times three steps, so a
legitimately in-flight attempt is never preempted.

## Contact identity

HubSpot deduplicates on email, and only on email. Most callers give a phone
number and no email, so the adapter matches by phone explicitly before
creating — skip that and every returning caller gets a second contact.

## Configuration-driven mapping

Nothing about a tenant's vertical is in code. `tenants.settings.crm`:

```yaml
crm:
  enabled: true
  deal_pipeline: sales
  deal_title: "{service} — {name}"
  stage_by_outcome:
    appointment_booked: appointmentscheduled
    lead_qualified: qualifiedtobuy
    default: new
  contact_properties:        # qualification answer key -> CRM property name
    pain: urgency
    insurance: insurance_provider
  deal_min_score: 30          # or: deal_on_outcomes: [appointment_booked]
```

Only explicitly mapped qualification answers are ever sent. Writing to a
property the tenant never created fails the whole sync in HubSpot, and
inventing fields in someone's CRM is worse than omitting them — so an
unmapped answer is dropped, not guessed.

Not every call earns a deal: `should_create_deal` defaults to "a booked
appointment always does; a qualified lead does above `deal_min_score`" — a
deal per call would flood the pipeline with wrong numbers and hang-ups. A
tenant can override with an explicit `deal_on_outcomes` list, or disable
deals entirely with `deal_on_outcomes: []`.

## Failure visibility

A permanently failed sync is marked `status = 'dead'` in `crm_syncs` by
`mark_dead`, called from the worker's last attempt. This matters because the
queue's own dead-letter list lives in Redis, invisible to anything outside
the process — `crm_syncs` is what lets a human (or Phase 7's dashboard) see
what needs attention. `failed_syncs()` is that query.

**Configuration:**

| Setting | Where | Notes |
| --- | --- | --- |
| `HUBSPOT_CLIENT_ID` / `_SECRET` | env | OAuth app credentials |
| per-tenant OAuth token | `integrations` row, provider `hubspot` | Fernet-encrypted, refreshed automatically |
| `crm.enabled` | tenant settings | default true; `false` skips sync entirely for that tenant |
| `crm.*` mapping | tenant settings | see above |

**Future improvements:** Pipedrive/Zoho as second adapters (interface is
already vendor-neutral); a dashboard view over `failed_syncs()`; deal
amount/close-date from tenant pricing config; two-way sync (CRM stage
changes reflected back, e.g. to trigger a reminder).

**Known risks:**
- The HubSpot adapter is contract-tested against a mock transport, not live
  HubSpot (`tests/test_hubspot.py`). Verify during the M4 live-sync test.
- `CLAIM_STALE_AFTER` is a judgment call (5 minutes). Too short risks two
  attempts genuinely overlapping under a slow HubSpot; too long delays
  recovery from a crashed worker. Revisit if either is observed.
- No two-way sync: a deal a human edits in HubSpot is never read back.
