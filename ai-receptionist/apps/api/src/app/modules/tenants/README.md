# tenants

**Purpose:** Tenant identity and configuration: number → tenant resolution,
versioned agent configs, and assembly of the runtime `AgentDefinition`
(system prompt with guardrails, greeting, voice) that the voice vendor runs.

**Dependencies:** `core` (db, config). Exposes types consumed by `telephony`.

**Inputs:** inbound phone numbers (E.164), tenant/agent rows seeded via
`scripts/seed_demo.py` (dashboard CRUD lands in Phase 7).
**Outputs:** `Tenant`, `PhoneNumber`, `AgentConfig` models; `AgentDefinition`.

**Configuration:** none beyond `core` settings; per-tenant behavior lives in
the database (see `config/tenants/*.yaml` for the documented surface).

**Keeping the phone agent current (`voice_sync.py`):** the voice vendor runs
a *copy* of the agent. A dashboard save diffs the `AgentDefinition` before and
after; if it changed, `tenants.voice_sync_requested` is bumped and a job on
`queue:voice_sync` (consumed by the crm worker) pushes it. The job records the
version it read *before* building the agent, so a save that lands mid-push
stays "updating" until its own job runs, and a job that finds its version
already live skips the vendor. On the last failed attempt it records the error.
`GET /tenants/me/voice-sync` turns this into live / updating / failed /
not_connected for the Settings page, and `POST` retries. Numbers are
re-attached when the vendor replaces a deleted agent. First-time connection
stays a deliberate `make provision`. `PATCH /tenants/me` also refuses an
unknown timezone (422), since bookings and the agent's clock both depend on it.

**Security note:** number resolution runs before tenant identity exists, so
`phone_numbers` carries a SELECT-only RLS policy permitting lookup from
unscoped sessions (migration 0002). No other table is readable unscoped.

**Future improvements:** tenant CRUD API + onboarding flow; prompt templating
per vertical; config validation against a JSON schema.

**Known risks:** a push failure never fails the save; the setting is stored
and Settings shows "not updated" with a retry until it lands. Prompt assembly is deliberately simple (persona + guardrails
+ escalation); qualification-flow injection into the prompt arrives with the
conversation engine in Phase 4.
