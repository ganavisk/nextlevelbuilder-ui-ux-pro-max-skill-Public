# AI Receptionist Platform — Phase 5: Scheduling

**Status:** Delivered — awaiting product-owner approval before Phase 6 (CRM)
**Milestone:** M3 "Booking" — code-complete and integration-tested against live Postgres +
Redis. Live-calendar validation still waits on Google OAuth credentials.

---

## 1. What Phase 5 delivers

A caller can ask for a time, be offered real availability, book it, and receive email and
SMS confirmations — with double-booking prevented under genuine concurrency.

| Component | Where | Verified |
|---|---|---|
| **Availability maths** (business hours, durations, notice, DST) | `scheduling/availability.py` | ✅ 13 unit |
| **Booking service** — the double-booking guarantee | `scheduling/service.py` | ✅ 7 integration (real Postgres) |
| **Google Calendar adapter** behind `CalendarProvider` | `scheduling/providers/google.py` | ✅ 23 contract (mock transport) |
| **In-call tools**: `check_availability`, `book_appointment` | `scheduling/tools.py` | ✅ 15 unit + integration |
| **Notification dispatch** — claim-then-send | `notifications/service.py` | ✅ 7 integration |
| **Resend (email) + Twilio (SMS) adapters** | `notifications/providers/` | ✅ via fakes |
| **Confirmation templates** (email + SMS, opt-out) | `notifications/templates.py` | ✅ 2 unit |
| **Confirmations worker** | `workers/confirmations.py` | ✅ E2E, incl. concurrent redelivery |
| **Tenant isolation guard** (see §5) | `tests/test_tenant_isolation.py` | ✅ 4 integration |
| **Model registry** | `app/models.py` | ✅ |
| Migration 0005 (booking integrity) | `migrations/versions/0005_*.py` | ✅ applied, indexes verified |

**Test count: 123 (up from 100 at Phase 4 close, 43 at Phase 4 delivery).** No live vendor
credentials are needed: the calendar adapter is driven against `httpx.MockTransport`, and
everything else runs against real Postgres + Redis with fake providers.

## 2. How a booking call now flows

```
Caller: "Can I come in Tuesday morning?"
   └─▶ Vapi calls check_availability(service, preferred_time)
        └─▶ find_available_slots:
             business hours (tenant, local tz)
               MINUS calendar busy periods (Google freeBusy)
               MINUS our own confirmed appointments   ← the in-flight ones
             └─▶ "I have Tuesday at 9 am, Tuesday at 10 am, or Wednesday at 9 am."

Caller: "Nine works."
   └─▶ Vapi calls book_appointment(service, starts_at, customer_name, ...)
        └─▶ 1. INSERT appointment  ← the lock; loser is offered alternatives
            2. Google events.insert
            3. release the row if (2) failed
        └─▶ enqueue queue:confirmations          (async — caller does not wait)
        └─▶ "You're all set for Tuesday at 9 am. I'll send you an email and a text."

                    confirmations worker
                      ├─ claim email row (unique key) ─▶ Resend
                      └─ claim sms   row (unique key) ─▶ Twilio
```

## 3. The double-booking guarantee, concretely

No calendar API offers an atomic "create this event only if the slot is free". Two callers
can pass an availability check milliseconds apart and both attempt to book. The calendar
therefore **cannot** be the arbiter — our database is:

```sql
CREATE UNIQUE INDEX appointments_no_double_booking
    ON appointments (tenant_id, starts_at) WHERE status = 'confirmed';
```

The ordering in `book_appointment` *is* the design:

1. **Insert the row** — this is the lock. The loser gets a unique violation.
2. **Write the calendar** — only once the slot is held.
3. **Release** — if (2) fails, so an outage doesn't leave the slot phantom-booked.

A second unique index on `idempotency_key` distinguishes a *retry* from a *race*: both
collide on the same slot, but only a retry carries a key we've already seen. A redelivered
webhook returns the original appointment; a genuine second caller is told the time is taken.

Because the index is partial on `status = 'confirmed'`, cancelling frees the slot with no
extra bookkeeping.

**Proof, not assertion.** `test_ten_concurrent_callers_produce_exactly_one_booking` runs ten
simultaneous bookings against live Postgres: exactly one succeeds, nine raise `SlotTakenError`,
and the calendar is written to once. I verified the test is not vacuous by dropping the index
and re-running — all ten then booked the same slot, as expected.

## 4. Verification performed (this session)

- **123 tests pass**; `ruff check` and `ruff format --check` clean.
- Migration 0005 applied against live Postgres; both partial unique indexes confirmed present
  via `\d appointments`.
- Concurrency test verified non-vacuous by index removal (above).
- Confirmation idempotency proven two ways: sequential redelivery (3× the same job → 1 email,
  1 SMS) and concurrent redelivery (2 workers racing → 1 email, 1 SMS).
- Release-on-failure proven end-to-end: a failing calendar write leaves zero confirmed rows,
  and the next caller can take the slot.
- Calendar adapter contract-tested against mock transport: freeBusy request shape and parsing
  (both `Z` and `+00:00` spellings), per-calendar `errors` arrays, 409-as-existing, 404/410
  cancellation tolerance, token refresh, refresh persistence, revoked-grant marking.

## 5. Defects found and fixed this phase

**RLS was not actually enforced in the dev environment.** The official Postgres image makes
`POSTGRES_USER` a superuser, and **superusers bypass row-level security unconditionally** —
`FORCE ROW LEVEL SECURITY` does not apply to them. Every policy still printed correctly in
`\d`, isolation tests still passed, and the tenant boundary was simply absent. I found it only
because a retrieval test showed one tenant's knowledge chunk surfacing in another tenant's
results.

Fixed by bootstrapping the container as `postgres` and having the application connect as a
separate `NOSUPERUSER NOBYPASSRLS` role created by `infra/postgres/init/00-app-role.sql`.
`tests/test_tenant_isolation.py` now asserts the *mechanism*, not just its outcomes: the app's
role cannot bypass RLS, every tenant table has RLS both enabled **and** forced, and cross-tenant
read and forged insert both fail. This one would have shipped.

**Eager cipher construction broke startup.** `GoogleCalendarProvider.__init__` built its
`CredentialCipher` immediately, so the app failed to *boot* anywhere `CREDENTIALS_ENCRYPTION_KEY`
was unset — even for deployments that never book. Now constructed on first use.

**`Base.metadata` depended on import order.** No module imported all ORM models, so a process
that imported only some of them raised `NoReferencedTableError` on the first FK flush. Added
`app/models.py`, imported by the API, both workers, the scripts, and the test suite.

**Idempotency keys could collide across callers.** With no call id to scope them, the key
reduced to the slot alone — so two unrelated callers requesting the same time looked like one
retry, and the second would have been told they were booked into the first caller's
appointment. Now unique per attempt when there is no call id.

**Flaky test fixture.** `FakeEmbeddingProvider` bucketed words with the builtin `hash()`, which
Python randomizes per process, so distance-threshold assertions passed or failed at random.
Switched to a stable digest.

## 6. Design notes for the reviewer

- **`CalendarProvider.list_available_slots` became `list_busy_periods`.** Business hours, service
  durations, and notice periods are platform policy, not vendor data. Leaving them in the adapter
  would have duplicated them in every future vendor and made them untestable without a live
  calendar. The adapter now returns raw free/busy and nothing else.
- **Half-open intervals.** A slot ending exactly when a busy period starts is not a conflict —
  otherwise the practice loses one appointment at every boundary.
- **Timezones are a correctness issue, not a formatting one.** Business hours are local to the
  tenant; slots are computed in that local frame and returned UTC-aware. There is an explicit DST
  test covering the November transition, where the same 9am local opening is a different UTC hour
  either side.
- **Every in-call failure is speakable.** A calendar outage becomes "let me take your details and
  have someone call you back", a lost race becomes "that one was just taken — I do have…". No
  error text and no silence ever reaches a caller (Risk R9).
- **Confirmations never block the call.** They are enqueued and sent by a separate worker, run as
  its own process so it keeps draining when post-call summarization is backed up behind a slow LLM.

## 7. What M3 still needs from you (for the live demo)

| Need | Why | Where it goes |
|---|---|---|
| Google OAuth client id + secret | per-tenant calendar connection | `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` |
| A Google account to connect | the practice's actual calendar | `integrations` row, via the connect flow (Phase 7 UI; seedable by hand until then) |
| Resend API key + verified sender domain | email confirmations | `RESEND_API_KEY`, `EMAIL_FROM` |
| Twilio messaging service SID | SMS confirmations | `TWILIO_MESSAGING_SERVICE_SID` |

Until these exist, booking works end-to-end against the database and is proven by the test suite;
only the vendor round-trips are unexercised.

## 8. Known gaps carried into Phase 6

- Google and Resend/Twilio adapters are contract-tested, not live-tested. Their wire formats are
  locked down but unverified against the real services.
- A `failed` notification row is terminal — nothing retries it and nothing alerts on it. Acceptable
  at zero volume; the first thing to fix before onboarding a real practice.
- `(tenant_id, starts_at)` assumes one bookable resource per tenant. A practice with two chairs
  needs a resource id in that index or it will under-book.
- No rescheduling or cancellation tool yet, though `CalendarProvider.cancel` exists and is tested.
- Availability hits the calendar on every check; at volume this needs a short cache, which the
  step-1 conflict check makes safe to add.

## 9. Next: Phase 6 — CRM (on your approval)

HubSpot behind a `CRMProvider`: contact upsert on qualified leads, appointment and call activity
sync, and the same idempotency discipline applied to an API that has its own opinions about
duplicates.
