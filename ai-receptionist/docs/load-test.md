# Load test: the live call path

**Question:** how does the part of the platform a caller waits on (the Vapi
webhook and the tools behind it) hold up under concurrent calls, and does
booking stay correct under contention?

**Short answer:**
- **Correctness:** booking stayed correct in every run we checked: no bay double-booked,
  and no slot over capacity.
- **Latency:** with 4 API processes and 25 callers going back to back, the
  steps a caller waits on stayed under 0.4 s at p95.
- **Throughput:** one process tops out around 13 calls/s; more processes
  scale it until the machine saturates.
- **A bug found and fixed:** a call whose start event never arrived turned
  every later tool call into a 500, which the caller would hear as silence.

## Method

`apps/api/scripts/loadtest.py` drives virtual callers through
`POST /webhooks/voice/vapi`, signed with the webhook secret, exactly as Vapi
does. Each call:

```
call started → check_availability → record_qualification_answer
→ book_appointment → (every 5th call) request_callback → end-of-call report
```

There is no think time between steps: each virtual caller is a worst case
compared with a real call, which sends 5–8 webhooks over a few minutes.

Bookings compete for **12 slots × 3 bays = 36 places** on purpose, so most
callers lose the race. That exercises the overlap-proof booking path under
contention (per-bay advisory lock, exclusion constraint, alternatives
search). Afterwards the script checks Postgres directly:
- confirmed bookings equal the callers told "all set";
- zero overlapping bookings in any bay, and no slot over capacity;
- every callback is saved.

The server is `apps/api/scripts/loadtest_server.py`: the production app with
only the Google Calendar client swapped for an in-memory one. Everything else
is real: secret check, Redis call state, Postgres with row-level security and
the booking constraints, the tool executor, and queueing of post-call and
confirmation jobs.

**Environment:** one 4-vCPU, 15 GB machine running Postgres 16, Redis, the
API and the load generator together. So these numbers are pessimistic for a
deployment where they run on separate machines. Python 3.12, uvicorn with 1
or 4 processes, default database pool (5 + 10 per process).

## Results (after the fix)

Each run is 4 × concurrency calls. Latency in milliseconds, p50/p95.

| API processes | Concurrent callers | Calls/s | Webhooks/s | check_availability | booking, booked | booking, refused | Errors |
|---:|---:|---:|---:|---|---|---|---:|
| 1 | 10 | 11.2 | 58 | 143 / 241 | 249 / 421 | 316 / 477 | 0 |
| 1 | 25 | 12.3 | 64 | 423 / 516 | 597 / 850 | 738 / 990 | 0 |
| 1 | 50 | 13.8 | 72 | 585 / 1080 | 794 / 1755 | 1528 / 2374 | 0 |
| 1 | 100 | 13.4 | 70 | 1110 / 2646 | 1919 / 3621 | 3411 / 6161 | 0 |
| 4 | 10 | 21.1 | 110 | 72 / 232 | 40 / 256 | 272 / 307 | 0 |
| 4 | 25 | 33.6 | 175 | 72 / 399 | 92 / 323 | 190 / 654 | 0 |
| 4 | 50 | 35.0 | 182 | 134 / 718 | 180 / 324 | 240 / 1477 | 0 |
| 4 | 100 | 20.2 | 105 | 581 / 2662 | 195 / 1088 | 1111 / 3451 | 1 (client `ReadError`) |

With plenty of free slots (120 slots, 25 concurrent), nearly every caller
booked first time:
- 1 process: booking p50/p95 420/660 ms
- 4 processes: booking p50/p95 203/710 ms

**Integrity, the 10 runs above (1,680 calls):** 0 bay overlaps, 0 slots over
capacity, and confirmed bookings always equal to the callers told "all set".
Eight more runs at 100 concurrent and 4 processes (3,200 calls), made while
investigating the `ReadError` below, were checked for errors only.

**Variance is large.** Identical 4-process runs at 100 concurrent took 12 to
24 s, because the generator, API and database compete for the same 4 cores.
Read the table for shape, not decimals.

## Findings

1. **Fixed: a lost or late call-start event broke the whole call.** Tool
   calls and the end-of-call report looked the call up, and failed with a
   500 if its start event hadn't been recorded. Vapi doesn't guarantee order
   or delivery. The first event for an unknown call now registers it
   (`telephony/service.py: _register_call`, logged as
   `call_registered_late`). There's a regression test for a call whose start
   event never arrives.
2. **Booking is correct under contention.** The database never
   double-booked a bay or overfilled a slot, including 400 callers (100 at a
   time) racing for 36 places.
3. **A refused booking costs 2–4 times a successful one.** It has tried
   every bay, then searched two weeks for alternatives to offer. That's fine:
   it's rare in real life and still bounded by the tool deadline (8 s).
4. **One process is CPU-bound at about 13 calls/s.** Four processes roughly
   double to triple that until the shared machine saturates, at 100
   concurrent here. Scale with `WEB_CONCURRENCY` first.
5. **Database connections need a budget.** Each process may open 15
   connections. Four API processes plus three workers can ask for 105, more
   than many managed Postgres plans allow. `DB_POOL_SIZE` and
   `DB_MAX_OVERFLOW` are now settings; the formula is in the Railway README.
6. **Unexplained:** one client-side `ReadError` (connection dropped while
   reading) in about 19,000 requests at 100 concurrent, with no error on the
   server. A keep-alive race is plausible, but 6 reruns with and without a
   longer keep-alive didn't reproduce it, so nothing was changed. Watch for
   it in production logs.

The first matrix, before the fix, also had 10 client-side errors whose kinds
weren't recorded yet (now they are). They didn't recur.

## What this doesn't measure

- **External services:** Google Calendar, Vapi and Claude latency. The tool
  deadline (8 s) caps how long a caller can wait on them.
- **`answer_faq`**, which needs Voyage and Claude; it was left out.
- **Railway's own network and CPU sizes.** To measure those, rerun on a
  staging deployment of `loadtest_server.py` with a staging database.

## Rerun it

```bash
cd apps/api
# terminal 1: the real API with an in-memory calendar (never deploy this)
VAPI_WEBHOOK_SECRET=loadtest uvicorn scripts.loadtest_server:app --port 8100 --workers 4
# terminal 2: needs DATABASE_URL / REDIS_URL for the same database
VAPI_WEBHOOK_SECRET=loadtest python scripts/loadtest.py --api http://localhost:8100 \
    --calls 100 --concurrency 25 --slots 12 --label "(4 workers)"
```

It prints the tables above for that run, exits non-zero on any error or
integrity failure, and removes its tenant, rows and queued jobs.
