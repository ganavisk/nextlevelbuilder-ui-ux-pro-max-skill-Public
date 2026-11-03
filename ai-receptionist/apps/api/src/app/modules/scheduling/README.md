# scheduling

**Purpose:** Find open appointment times and book them, without ever selling
the same slot twice. Owns availability policy (business hours, service
durations, notice periods), the `appointments` write path, and the conflict
prevention that makes concurrent callers safe. Calendar vendors sit behind
`CalendarProvider` and are replaceable.

**Dependencies:** `core` (db, config, logging, security, queue),
`tenants` (the `Tenant` row supplies timezone, business hours, and the
`settings.scheduling` policy block). Called from
`conversation.tools.ToolExecutor` via the `check_availability` and
`book_appointment` tools. Enqueues onto `queue:confirmations`, which the
`notifications` module consumes — it does not call that module directly.

**Inputs:** a tenant, a requested service, an optional preferred time (ISO
8601 in the practice's local time), and the caller's name/phone/optional
email.
**Outputs:** spoken-language strings for the voice agent; an `appointments`
row; a calendar event; a queued confirmation job.

## The double-booking guarantee

This is the part worth understanding before changing anything here.

No calendar API offers an atomic "create this event only if the slot is
free". Two callers can pass an availability check milliseconds apart and both
try to book. So the calendar cannot be the arbiter — our own database is.
The serialization point is an exclusion constraint (migration 0008, which
replaced the original per-start-time unique index from 0005):

```sql
ALTER TABLE appointments ADD CONSTRAINT appointments_no_overlap_per_bay
    EXCLUDE USING gist (
        tenant_id WITH =, bay WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (status = 'confirmed');
```

It refuses any two confirmed appointments that *overlap* in the same bay, not
only ones that start at the same instant, so a 90-minute brake job can't be
double-booked by an oil change starting half an hour later. It needs the
`btree_gist` extension (trusted, created by `infra/postgres/`).

`service.book_appointment` therefore runs in this order, and the order is the
design:

1. **Insert the appointment row.** This is the lock. The loser of a race gets
   a constraint violation and is offered alternatives.
2. **Write to the calendar,** only once the slot is held.
3. **Release the reservation** if that write fails, so a vendor outage does
   not leave the slot phantom-booked.

Doing it the other way round (calendar first) cannot be serialized. Skipping
step 3 is how "ghost" appointments appear — held in our table, absent from
the practice's calendar.

Because the constraint is partial on `status = 'confirmed'`, cancelling an
appointment frees the slot immediately with no extra bookkeeping.

## Service bays (parallel capacity)

A tenant with `scheduling.bays: N` in its config (an auto shop's lifts; any
business with several identical resources) can take N overlapping
appointments. Single-resource tenants omit it and behave exactly as before
(one bay).

- **Availability** offers a slot while *any* bay is free for its whole
  duration (`availability.free_bay`).
- **Booking** tries bay 1, 2, ... N, each attempt in its own transaction, and
  takes the lowest free one; when every bay refuses, the caller is offered
  alternatives. Each attempt first takes
  `pg_advisory_xact_lock(hash(tenant, bay))`: concurrent inserts that conflict
  under an exclusion constraint otherwise *deadlock* rather than fail cleanly
  (seen intermittently with four racers in `tests/test_bays.py`; none in 25
  runs with the lock).
- **Calendar.** With more than one bay, events are written
  `transparency: transparent` and titled "Bay N · ...", so the shop's single
  Google calendar keeps a full record without its free/busy blocking the whole
  shop after one booking. Capacity lives in our table, not in free/busy.

Per-tenant `scheduling.booking_fields` (e.g. vehicle, mileage, concern) are
added to the `book_appointment` tool schema for that tenant only and stored
in `appointments.details` (JSONB). No vertical-specific code.

## How times are spoken

Tool results are read aloud, so `availability.py` formats them for a listener,
not a reader:

- **Days relative to today**, in the business's timezone: "today",
  "tomorrow", a weekday within the coming week, and a date beyond that
  ("Tuesday, November 10th"). A bare weekday a week out is heard as *this*
  one. Confirmations always include the date.
- **Offers that are real choices** (`pick_offers`): at least 90 minutes apart,
  centred on the time the caller asked for, the day named once ("tomorrow at
  8:30 am, 10 am, or 11:30 am"), and "noon" for 12 pm. If the requested time
  is open, the answer is just "Yes, ... is open."
- **Read-back on booking**: the service, the tenant's `confirm_detail`
  booking field (an auto shop's vehicle), the date and time, and where the
  confirmation goes ("the number you're calling from"). This is the caller's
  last chance to catch a misheard detail.

## Idempotency

A second unique index on `idempotency_key` distinguishes a *retry* from a
*race*. Both collide on the same slot, but only a retry carries a key we have
already seen — so a redelivered webhook returns the original appointment
while a genuine second caller is told the time is taken. Keys are scoped to
the call and the slot (`call:<id>:<iso-start>`); without a call id there is
no retry identity, so the key is made unique per attempt instead (keying on
the slot alone would let one caller be booked into another's appointment).

The Test Lab tool bench passes its own scope per run (`bench:<uuid>`), since
every bench booking is deliberate: four runs at one slot must fill three bays
and be refused once, not replay the first booking three times.

## Hot path

`check_availability` and `book_appointment` run while the caller is waiting,
inside the <2s tool budget (Phase 1 §3.3). Calendar HTTP calls carry a 5s
timeout, and every failure mode degrades to something a receptionist would
plausibly say rather than an error — a calendar outage becomes "let me take
your details and have someone call you back" (Phase 1 Risk R9).

**Configuration:** per-tenant, in `tenants.settings.scheduling`:

```yaml
scheduling:
  services:
    - { name: "Cleaning & check-up", duration_minutes: 60 }
    - { name: "Emergency exam", duration_minutes: 30 }
  min_notice_hours: 2
```

Service matching is deliberately loose — callers say "a cleaning", not
"Cleaning & check-up" — and falls back to the first configured service rather
than refusing. Business hours and timezone come from the `tenants` row.
Google credentials are per-tenant rows in `integrations`, Fernet-encrypted
with `CREDENTIALS_ENCRYPTION_KEY`, which lives only in the environment.

**Future improvements:** rescheduling and cancellation as in-call tools
(the provider already supports `cancel`); Cal.com as a second adapter;
per-provider/per-room resources rather than one calendar per tenant;
buffer time between appointments; waitlists when the horizon is full.

**Known risks:**
- The Google adapter is contract-tested against a mock transport, not live
  Google (`tests/test_google_calendar.py`). The wire format is locked down but
  unverified against the real API until the M3 live-booking test.
- Availability reads the calendar on every check. At high call volume this
  will need a short-lived cache, which reintroduces staleness — the DB-side
  conflict check in step 1 is what keeps that safe.
- `(tenant_id, starts_at)` assumes one bookable resource per tenant. A
  practice with two chairs needs the index widened to include a resource id,
  or it will under-book.
