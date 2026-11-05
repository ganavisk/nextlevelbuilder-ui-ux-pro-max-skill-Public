# AI Receptionist Platform — Phase 8: Production Readiness

**Status:** Delivered. It needs product-owner approval and a staging deploy before the first real tenant.
**Milestone:** M6 "Production". The exit criterion is that a second business onboards with zero code changes and its data stays isolated. That is proven by integration tests against real Postgres and Redis (§2). Deployment is documented and the image is smoke-tested. The live Railway deploy itself was not done from this environment (§5).

---

## 1. What Phase 8 delivers

| Component | Where | Verified |
|---|---|---|
| **Production image**: one non-root, multi-stage image for the api, the 3 workers and migrate | `apps/api/Dockerfile`, `docker-entrypoint.sh` | ✅ `infra/scripts/smoke-image.sh` in CI |
| **RLS startup guard**: refuses a superuser or BYPASSRLS role in staging and production | `core/db.py`, `infra/postgres/bootstrap-managed.sql` | ✅ against a real superuser and the bootstrapped role |
| **Login throttle**, per account and per IP, plus the timing-leak fix | `tenants/login.py` | ✅ route tests incl. parallel burst |
| **Sentry** that can't leak call data (nothing collected, scrubbed, no 4xx) | `core/observability.py` | ✅ end-to-end leak test |
| **Bounded in-call latency** (SDK timeouts; a deadline on read tools) | `core/config.py`, `conversation/tools.py` | ✅ |
| **Idempotent end-of-call and post-call handoffs** | `telephony/service.py`, `workers/post_call.py` | ✅ redelivery tests |
| **CI**: api as the restricted role, `mypy --strict`, dashboard build, image smoke | `ai-receptionist/.github/workflows/ci.yml` | ✅ |
| **Railway and Vercel deploy guide**, security headers, connection budget | `infra/railway/README.md`, `apps/dashboard/vercel.json` | 📄 written; not run against Railway (§5) |
| **Load test** of the live call path, with integrity checks | `scripts/loadtest*.py`, `docs/load-test.md` | ✅ 0 errors, 0 overlaps after the fix |
| **Operations runbook** and **dead-letter requeue tool** | `docs/runbooks/operations.md`, `scripts/requeue_dead.py` | ✅ 4 tests (atomic move) |
| **Postgres image** (pgvector, pgBackRest, built-in backup scheduler) for self-run Postgres | `infra/postgres/image`, `docs/runbooks/postgres-image.md` | ✅ 517 tests against it, CI job, restore-drilled |
| **Tenant #2 proof** (zero code, isolated) | `tests/test_second_tenant.py` | ✅ 3 tests (§2) |
| **Database review**: an RLS audit, a retrieval-recall bug and hot-path indexes | migration `0012`, `retrieval.py`, `core/db.py` | ✅ EXPLAIN-measured (§3) |

Phase 8 also delivered product work that came out of using the system: service bays, the auto repair tenant, the Test Lab, voice-agent sync and a voice review (§6).

**Backend test count: 520, up from 259 at Phase 7 close.** `ruff check`, `ruff format` and `mypy --strict` are clean, and the dashboard `next build` is clean.

## 2. Tenant #2: onboarding by configuration

The claim is that a new business is a YAML file, not a code change. Tenant #1 is the dental practice and tenant #2 is the auto repair shop. They differ in exactly the ways that would tempt someone to branch on tenant:
- 3 parallel service bays, against 1 chair;
- a vehicle field collected at booking and read back;
- a safety-first breakdown flow;
- different services, hours, triggers and knowledge.

`tests/test_second_tenant.py` proves it three ways.

1. **Statically.** Nothing under `src/app` contains a quoted slug, name, vertical or service name from any `config/tenants/*.yaml`.
   - The scan reads the YAML, so a new example tenant is covered automatically.
   - On its first run it found the string `"A/C performance check"` in a docstring in `scheduling/tools.py`. The string was harmless and has been replaced, but it shows the check works.

2. **End to end.** Both businesses are onboarded by the real onboarding path, `scripts/seed_demo.py: seed`, from their example YAML. Only the slug, number and login are changed. Then they take calls through the real signed Vapi webhook:
   - The dental practice books and takes a callback.
   - The auto shop books two separate callers into the same 10:00 slot, which works because it has 3 bays. The confirmation reads back "for your 2014 Ford F-150", the shop's own booking field.
   - Each owner signs in to the dashboard API and sees only their own business: calls 1 and 3, appointments 1 and 2, callbacks 1 and 1.
   - The dental owner gets 404 on the auto shop's call and on cancelling its appointment. The appointment is still confirmed afterwards.
   - **Database layer.** The test runs inside a tenant #1 session. For every table in `public` that has a `tenant_id` column, it counts rows belonging to any other tenant, and every count is 0. The table list comes from `pg_class`, not a hand-kept list, so a table added later is covered automatically. There are 15 such tables today.

3. **Knowledge retrieval.** Each business is asked the other's exact chunk text: distance 0, the strongest pull there is. It gets none of the other business's chunks. Its own text returns its own chunk first.
   - This test exists because of the recall bug in §3.2.
   - It checks isolation, not ranking quality. The ranking itself is unchanged.

`tests/test_tenant_isolation.py` gained two catalog-driven guards:
- every tenant table has RLS enabled and forced, with a `tenant_isolation` policy using both USING and WITH CHECK. Removing FORCE from one table makes this test fail, which was checked;
- every app session plans with real values (§3.4).

**One limit on "zero code":** `tenants.vertical` has a check constraint (`dental`, `legal`, `auto_repair`, `other`). A plumber onboards today as `other`, with no code. Giving plumbing a named vertical would need a migration. Nothing in the code branches on `vertical`.

## 3. Database review

This section is the database review, done as a DBA would. Each finding was confirmed with `EXPLAIN (ANALYZE, BUFFERS)` against Postgres 16 with pgvector 0.6.0, on seeded volumes. The fix is stated first, then the evidence, then the tradeoff. Migration `0012_hot_path_indexes` carries the index changes.

### 3.1 Row-level security: sound, with one table out of scope by design

- **Coverage.** All 15 tables with a `tenant_id` have RLS **enabled and forced**. Each has a `tenant_isolation` policy:
  ```sql
  tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
  ```
  The policy applies to both reads (USING) and writes (WITH CHECK), so a row can't be written into another tenant either.
- **No tenant set means no rows.** An unset or empty `app.tenant_id` becomes NULL, so the policy matches nothing.
- **Transaction-scoped.** The tenant is set with `set_config(..., true)`. A pooled connection therefore can't carry one tenant's setting into the next transaction.
- **The application role can't bypass it.** The app connects as `receptionist_app`, which is NOSUPERUSER and NOBYPASSRLS, and the API refuses to start as anything else. Because RLS is forced, it applies even to the table owner, which is also the migration role.
- **`tenants` has no RLS on purpose.** It's what login and webhook routing look up before a tenant is known. The application only reads it by slug, id or phone number, and the dashboard API only exposes the caller's own row. The tradeoff: a future query on `tenants` without a WHERE clause would see every business. The integration test covers the endpoints, not every possible future query.

### 3.2 Fixed: a tenant could get none of its own knowledge back

**Fix.** Retrieval now filters to the tenant first, in a `MATERIALIZED` CTE, and computes exact cosine distance over just that tenant's chunks. The global HNSW index is dropped.

**Mechanism.** The old query ordered all of `knowledge_chunks` by `embedding <=> $q` and let RLS filter the result.
- With the HNSW index, Postgres walks the graph across every tenant's vectors, collects `ef_search` (40) candidates, and only then applies the tenant filter.
- pgvector 0.6 has no iterative scan, so if all 40 candidates belong to other tenants the query returns **nothing**.
- The caller then hears "I don't have that information" about something in the knowledge base.

**Evidence.** I seeded 20,000 chunks across tenants and gave the target tenant a chunk at distance 0.011 from the query. With the index in use, the query returned **0 rows**.

**Cost of the fix.** Exact distance is linear in one tenant's chunk count, at about 7 µs per 1024-dimension chunk. It isn't affected by other tenants: each case below had a neighbour tenant holding 20,000 chunks.

| Chunks in this tenant | Execution |
|---:|---:|
| 50 (the size of the example tenants) | 0.26 ms |
| 500 | 3.2 ms |
| 5,000 | 35 ms |

The embedding API call in the same step takes hundreds of milliseconds, so this cost is noise at realistic sizes.

**Tradeoff.** A tenant with tens of thousands of chunks would pay tens of milliseconds per FAQ.
- The answer then is pgvector ≥ 0.8 iterative scans (`hnsw.iterative_scan`), or a per-tenant partial index.
- It is **not** to restore a global index, which brings the bug back.
- Dropping the index also removes its write and memory cost.

### 3.3 Hot-path indexes

Each index serves a path that was measured or that runs while a caller waits. All are built `CONCURRENTLY`.

| Path | Index | Before | After |
|---|---|---|---|
| `check_availability`, run on every slot check during a call | `appointments (tenant_id, ends_at) WHERE status = 'confirmed'` | 8.1 ms, 2,003 buffers | 0.12 ms, 7 buffers |
| Callback queue: open first, then newest (dashboard) | `callback_requests (tenant_id, (status = 'resolved'), created_at DESC)` | 3.8 ms: seq scan and top-N sort of all 5,000 | 0.045 ms |
| Appointments page | `appointments (tenant_id, starts_at DESC)` | — | 0.055 ms |
| Test Lab activity feed, polled every 3 s | `call_events (tenant_id, occurred_at DESC)` | — | 0.10 ms |
| Tenant-first retrieval (§3.2) | `knowledge_chunks (tenant_id)` | — | (in §3.2) |
| Test Lab discard; `ON DELETE SET NULL` from `calls` | `call_id`, partial `IS NOT NULL`, on 5 tables | seq scan each | index |

Notes on the measurements:
- **Availability.** Measured with 30,000 past bookings. Past bookings stay `confirmed`, so before the index the window query read a tenant's entire history, and that history grows forever. The partial index only holds confirmed rows, keyed by `ends_at`, so the query reads only bookings that haven't ended. I also tried an `&&` range rewrite and dropped it: 11 ms, slower than what it replaced.
- **Callback queue.** A plain `(tenant_id, created_at)` index can't supply an ORDER BY that leads with an expression. Measured, it still read and sorted every callback. The expression index matches the ORDER BY exactly, and the route now sends `'resolved'` as a SQL literal. A bound parameter wouldn't match the index expression.
- **A bug the review found:** the queue was ordered by `id`, a random UUID, so "newest first" was never true. It now orders by `created_at`, which was already in the table but not mapped in the model.

**Tradeoff.** Each index is write amplification on every insert into its table. The table that matters most is `call_events`, which every webhook writes to. One extra B-tree on two narrow columns is cheap next to the indexed read the Test Lab feed makes every 3 s. I did **not** index the `tenant_id` foreign keys on rarely deleted tables. Deleting a tenant will seq-scan them, which is acceptable for an operation that happens a handful of times a year.

### 3.4 Fixed: generic plans ignored the time window

**Fix.** The app's engine sets `plan_cache_mode = force_custom_plan` through asyncpg `server_settings`. `test_app_sessions_always_plan_with_actual_values` asserts it on a real session.

**Mechanism.** asyncpg prepares statements. After 5 executions, Postgres may switch to a **generic plan**, which is costed without the parameter values. For the availability query, the generic plan picked `appointments_tenant_starts_idx`, read all 30,000 of the tenant's bookings, and filtered afterwards. It did so even with the status as a literal, because the window bounds are still parameters.

**Evidence.** On the 7th execution:

| Plan | Time |
|---|---|
| Generic | 6.2 ms |
| Custom | 0.011 ms to execute, plus 0.067 ms to plan |

**Tradeoff.** Every statement is now planned with its real values. That costs tens of microseconds of planning per query, and no query in this app is planning-bound. In return, Postgres never silently switches to a plan that gets worse as data grows.

### 3.5 Fixed: onboarding wasn't atomic, and shutdown leaked connections

- **Onboarding.** `seed_demo.seed` committed the tenant in one transaction and its config, number, user and knowledge in later ones. A failure partway through left a half-onboarded business whose slug blocked a retry.
  - The id is now generated client-side, and everything commits in one `tenant_session` transaction, so a failure leaves nothing behind.
  - Knowledge embedding runs afterwards, because it calls an external API. A failure there leaves documents `pending`, and they can be re-ingested.
- **Shutdown.** The engine was never disposed on shutdown, so a stopping API dropped pooled connections without a clean close.
  - The lifespan now calls `dispose_engine()` after closing Redis.
  - This also fixed intermittent "attached to a different loop" errors in tests that start more than one app.

### 3.6 Capacity: the connection budget

Each process opens up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections, 5 + 10 by default. Keep this true:

```
(api instances × WEB_CONCURRENCY + 3 workers) × 15  <  max_connections
```

and leave headroom for migrations and operators. The formula is in the Railway README and the runbook. If a managed plan's limit is tight, put PgBouncer in **transaction** mode in front of Postgres. That works because the tenant is set transaction-locally (§3.1), which is exactly what transaction pooling needs.

### 3.7 Backup and recovery: pgBackRest to an offsite bucket, restores drilled

The procedure is in `docs/runbooks/backup-and-restore.md`, with the drill log.

**Fix.** **pgBackRest** backs up to an S3-compatible bucket in another region:
- a weekly full backup and daily block-level incrementals;
- continuous WAL archiving (`archive_timeout = 60s`);
- encryption on the database host (AES-256-CBC) and zstd compression before upload;
- a 14-day recovery window.

`infra/pgbackrest/` holds the scripts:
- `setup.sh` takes its settings from environment variables, keeps secrets in a `640 root:postgres` file, and runs `check` plus the first full backup;
- `backup.sh` is the scheduled backup;
- `backup-status.sh` is the monitoring check: 0, 1 or 2, bounded at 30 s;
- `restore.sh` restores to a time or to `latest`, always into a new directory, with archiving off on the copy.

`scripts/verify_restore.py` decides whether a restore may serve. It checks the schema, row-level security, the app role and the booking constraint. Given a manifest, it also compares full-row hashes for every table, business by business.

**Evidence** (2026-11-05, local Postgres 16, 165 MB, against an S3 API stand-in behind verified TLS):

| Scenario | Result |
|---|---|
| A bad script deletes 4 businesses | Restored from the bucket to just before the DELETE in **4.3 s**. 25/25 identical to the manifest. |
| Server crash, no warning | **52.1 s** of writes lost, within the 60 s bound. Restored `latest` in 4.8 s. |
| Bucket unreachable for 4 min | Commits continued; monitoring went CRITICAL in 30 s. Archiving caught up in 2 s with no gap. |
| Encryption | Every object is ciphertext; 0 plaintext hits. A wrong passphrase can't read the repository. |
| Size | A 165 MB full backup is stored as 32.4 MB. A daily incremental takes about 3 s. |

**Tradeoffs and what's still open.**
- **RPO ≤ 60 s costs a WAL segment a minute even when idle.** zstd makes that cheap. A tighter RPO needs streaming replication, not a smaller `archive_timeout`.
- **If the bucket is unreachable long enough to queue 4 GiB of WAL, pgBackRest drops WAL** rather than let the disk fill and stop Postgres. That's availability over recovery window, and it's why the CRITICAL alert matters.
- **The cipher passphrase is a single point of failure.** It needs escrow outside the host and the bucket's account, and it can't be rotated in place.
- **The bucket was a stand-in.** It was moto's S3 server behind nginx TLS, so real credential checks, a provider's own behaviour and two regions are unexercised. Repeat the drill against the real bucket.
- **Managed Postgres won't run pgBackRest.** Whether Railway's Postgres offers PITR is unverified. The alternative is now built: **`infra/postgres/image`** (Postgres 16.15, pgvector 0.6.0, pgBackRest 2.50, with a built-in backup scheduler). The full API suite passes against it (517), it's in CI, and it was restore-drilled as a copy and as a new primary (`docs/runbooks/postgres-image.md`). It hasn't been deployed on Railway yet.
- **The end-to-end RTO target (≤ 30 min) is proposed, not confirmed.** The cutover step hasn't been drilled.

Redis holds only queues and live call state. Losing it loses queued jobs (summaries, texts, CRM syncs), not records.

## 4. Load test (summary)

`docs/load-test.md` has the method and the full matrix. Virtual callers drive the real signed webhook path, from availability through qualification, booking and callback, with no think time. They compete for 36 places so that most of them lose the race. Afterwards, Postgres is checked for double-booked bays and over-full slots.

- **Integrity:** across 1,680 checked calls, there were 0 bay overlaps and 0 over-capacity slots. Confirmed bookings always equalled the callers told "all set".
- **Latency:** with 4 processes and 25 concurrent callers, each step a caller waits on stayed under about 0.4 s at p95.
- **Throughput:** 1 process is CPU-bound at about 13 calls/s; 4 processes scale to 21–35 calls/s.
- **Bug found:** a call whose start event was lost turned every later tool call into a 500, which the caller hears as silence. Fixed: the first event of an unknown call now registers it.
- **After migration 0012 and `force_custom_plan`:** I reran with 4 processes, at 25 and 50 callers. Results were 13.6–33.8 calls/s, with 0 errors and 0 overlaps in every run.
  - The variance comes from the shared 4-core machine running the database, the API and the load generator together.
  - There was no regression.

## 5. Deployment

`infra/railway/README.md` covers:
- the services (one image, four roles; only `api` migrates, in its pre-deploy step);
- the full variable matrix and the connection budget;
- the database bootstrap with the restricted role;
- update and rollback;
- Vercel for the dashboard, with CSP and security headers.

`docs/runbooks/operations.md` covers:
- callers hearing silence;
- an API that won't boot;
- calendar failures and stalled queues, with the safe requeue tool;
- failed voice sync and connection exhaustion;
- rotating every secret, with the effect of each rotation.

**Railway has deprecated Config as Code.** New services can't opt in, and `railway.toml` stops being read on **2026-12-01**. The replacement is Infrastructure as Code (`.railway/railway.ts`). Railway's documentation site was unreachable from this environment, so its syntax could not be checked. The `infra/railway/*.toml` files are therefore marked **reference only**: enter their settings in the dashboard, or port them once you have checked the current docs. Don't assume Railway reads them.

**Migration 0012 in production.** It builds indexes `CONCURRENTLY`, so writes aren't blocked, but each build waits for transactions already open to finish. Run it at a quiet time. If a build fails, it leaves an `INVALID` index that `IF NOT EXISTS` would skip. Find it with:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

Then drop it and re-run.

## 6. Also built in Phase 8

These came out of exercising the system rather than the original plan. Each was tested and checked in a real browser.

- **Service bays.** Parallel appointments per tenant (`scheduling.bays`). An exclusion constraint per tenant and bay makes overlaps impossible, not just identical start times. A per-bay advisory lock removed a deadlock that broke 15 of 15 concurrent race runs.
- **The auto repair tenant**, entirely in YAML. The platform gained only general pieces: per-tenant booking fields, neutral prompts, and exact values for choice questions.
- **Test Lab.** Simulated calls against the tenant's real prompt and tools, a tool bench, a bay board and a live activity feed. Discard removes everything a session made. There is also a **scenario runner** for escalation triggers (`make lab-scenarios`), judged on behaviour, not wording.
- **Dashboard redesign** in Apple's style, with automatic light and dark, every text pairing contrast-checked, and a chart built to the dataviz method.
- **Voice agent sync.** Saving Settings now pushes the change to Vapi, with versioning, a status display and retry. Before this, callers kept hearing the old agent until someone ran `make provision`.
- **The current date in the business's timezone** in the production prompt, through Vapi's per-call LiquidJS templates. Before this, real calls had to guess what "tomorrow" meant.
- **Voice review fixes:**
  - unambiguous spoken dates;
  - offers spaced at least 90 minutes apart;
  - no question asked twice;
  - a read-back that carries the date and the shop's detail;
  - a different callback or confirmation number, normalized and read back digit by digit;
  - a name field on callbacks;
  - a safety-first breakdown flow;
  - an honest after-hours callback promise ("when we open, tomorrow at 7:30 am").
- **Escalation policy in Settings.** The callback promise and the trigger situations are now editable, and validated. The triggers reach the prompt; before this they were stored but read by nothing.
- **`make check-keys`**, a read-only preflight of every provider key that never prints a key.

## 7. Verification

| Check | Result |
|---|---|
| `pytest`, live Postgres 16 + Redis, as the restricted role | **520 passed** |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean |
| Dashboard `next build` (strict type-check) | clean |
| Migrations 0008–0012: up, down, up | ✅ |
| Production image boots as each role; refuses a superuser | ✅ |
| Load test after 0012 (4 processes, 25 and 50 callers) | 0 errors, 0 overlaps |
| Every change to the voice and Settings path | Test Lab bench run, light and dark, 390 px |

## 8. Defects found and fixed in Phase 8

| Defect | Found by | Effect before the fix |
|---|---|---|
| RLS skipped silently under a superuser login | review | Managed Postgres's default login sees every tenant |
| HNSW post-filter recall (§3.2) | DB review, reproduced | A tenant gets no FAQ answers |
| Generic plan ignores the time window (§3.4) | DB review | Availability slows as history grows |
| Availability scanned all history (§3.3) | DB review | Same, at every slot check |
| Callback queue ordered by random UUID | DB review | "Newest first" wasn't |
| Non-atomic onboarding; undisposed engine (§3.5) | DB review | A half-onboarded tenant; unclean shutdown |
| Lost call-start event turned the call into 500s | load test | Silence on the line |
| Idle workers crashed (Redis timeout = pop window); SIGTERM ignored | production image | Restart loops; SIGKILL on every deploy |
| End-of-call redelivery 500; lost post-call handoff | review | Calls never summarized or synced |
| Unknown accounts answered login faster | review | Revealed which accounts exist |
| Exclusion-constraint deadlock under races | bay race tests | Concurrent bookings failed |
| No date in the production prompt | review | Real calls could book the wrong day |
| Settings didn't reach the live agent | review | The dashboard said "updated"; it wasn't |
| Triggers stored but unused; promises unvalidated | review | `{next_opne}` read to callers verbatim |

## 9. Known gaps and risks

- **Backups (§3.7):** pgBackRest to an encrypted offsite bucket is built and drilled. RPO 52 s measured; restore under 5 s. Still open before real data:
  - create the real bucket (another region, versioning on, no version-delete for the host's key), escrow the passphrase, and repeat the drill against it;
  - decide where Postgres runs: the provider's PITR, or `infra/postgres/image` (built, tested and drilled; not yet deployed on Railway);
  - drill the cutover;
  - confirm the proposed 30-minute RTO.
- **Railway IaC:** the deprecated Config as Code needs porting to `.railway/railway.ts` before 2026-12-01. Its syntax is unverified (§5).
- **No staging deploy yet.** The load-test numbers come from one shared machine. Rerun them on staging before launch.
- **`CREDENTIALS_ENCRYPTION_KEY` has no rotation tool.** Rotating it forces every tenant to reconnect Google and HubSpot.
- **`tenants` has no RLS** (§3.1), and **`vertical` is a check constraint** (§2).
- **Large knowledge bases** need pgvector ≥ 0.8 (§3.2).
- **The escalation scenario runner hasn't run against the real model**: there is no `ANTHROPIC_API_KEY` in this environment. Run `make lab-scenarios` with a key before trusting the triggers.
- **Summit Towing's number** (555-014-2200) is a fictional 555-01xx placeholder in the example auto tenant. A real shop needs its own.
- Carried from Phase 7:
  - no automated frontend tests;
  - hand-written frontend types;
  - no token revocation;
  - OAuth checked only up to the consent screen;
  - one unexplained client `ReadError` in about 19,000 requests under load.

## 10. Next

1. Create the production backup bucket and escrow the passphrase, run `infra/pgbackrest/setup.sh` against it, and repeat the restore drill there, including the cutover. Confirm the RTO target.
2. Deploy to staging, porting the Railway settings to IaC. Then rerun the load test and `make lab-scenarios` there.
3. Onboard the first real business from its YAML, using `make seed`, `make provision` and `make check-keys`.
