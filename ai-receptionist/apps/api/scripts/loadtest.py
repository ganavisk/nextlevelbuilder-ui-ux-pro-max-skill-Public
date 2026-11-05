"""Load test the live call path: concurrent simulated calls through the Vapi
webhook, with latency per step and a booking-integrity check afterwards.

Each virtual caller does what a real call does, through
POST /webhooks/voice/vapi with the shared secret:

    call starts -> check_availability -> record_qualification_answer
    -> book_appointment -> (every 5th call) request_callback -> end-of-call

Bookings deliberately compete for a small pool of slots on one day (3 bays
each), so the overlap-proof booking path is exercised under contention: more
callers than capacity must be told "just taken", and no bay may be double
booked.

    # terminal 1: the real API, calendar swapped for an in-memory one
    VAPI_WEBHOOK_SECRET=loadtest uvicorn scripts.loadtest_server:app --workers 4
    # terminal 2
    VAPI_WEBHOOK_SECRET=loadtest python scripts/loadtest.py --calls 400 --concurrency 50

Needs DATABASE_URL / REDIS_URL for the API's database (it creates a
throwaway tenant and removes it, its rows and its queued jobs afterwards).
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import redis
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.queue import CONFIRMATIONS_QUEUE, CRM_QUEUE, POST_CALL_QUEUE

TZ = "America/Denver"
BAYS = 3
SERVICE = "Oil change"
FAILURE_MARKERS = ("trouble reaching the calendar", "had trouble with that", "not able to do that")
STEPS = (
    "call_started",
    "check_availability",
    "record_qualification",
    "book_appointment",
    "  of which booked",
    "  of which refused (just taken)",
    "request_callback",
    "end_of_call",
)


# ── setup and teardown ──────────────────────────────────────────────────


def _engine(settings: Settings):
    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    return create_engine(url)


def create_tenant(settings: Settings) -> tuple[uuid.UUID, str]:
    """A 3-bay shop open around the clock, so slots exist whenever this runs."""
    tenant_id = uuid.uuid4()
    number = f"+1555{random.randint(2000000, 9999999)}"
    hours = {d: [["00:00", "23:59"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    settings_json = {
        "scheduling": {
            "bays": BAYS,
            "services": [{"name": SERVICE, "duration_minutes": 60}],
            "min_notice_hours": 0,
            "booking_fields": [{"key": "vehicle", "description": "Vehicle"}],
            "confirm_detail": "vehicle",
        }
    }
    engine = _engine(settings)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, business_hours, "
                "settings) VALUES (:id, :slug, 'Load Test Garage', 'auto_repair', :tz, "
                "CAST(:h AS jsonb), CAST(:s AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": f"loadtest-{tenant_id.hex[:8]}",
                "tz": TZ,
                "h": json.dumps(hours),
                "s": json.dumps(settings_json),
            },
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO agent_configs (tenant_id, version, is_active, system_prompt, "
                "first_message, qualification, escalation_policy) VALUES (:t, 1, true, "
                "'Load test.', 'Hello.', CAST(:q AS jsonb), CAST(:e AS jsonb))"
            ),
            {
                "t": str(tenant_id),
                "q": json.dumps([{"key": "vehicle", "question": "Vehicle?", "type": "text"}]),
                "e": json.dumps({"callback_promise": "We'll call you back."}),
            },
        )
        conn.execute(
            text("INSERT INTO phone_numbers (tenant_id, e164) VALUES (:t, :n)"),
            {"t": str(tenant_id), "n": number},
        )
    engine.dispose()
    return tenant_id, number


def integrity(settings: Settings, tenant_id: uuid.UUID) -> dict[str, int]:
    """Confirmed bookings, and how many bay overlaps exist (must be zero)."""
    engine = _engine(settings)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        confirmed = conn.execute(
            text("SELECT count(*) FROM appointments WHERE tenant_id = :t AND status = 'confirmed'"),
            {"t": str(tenant_id)},
        ).scalar_one()
        overlaps = conn.execute(
            text(
                "SELECT count(*) FROM appointments a JOIN appointments b "
                "ON a.tenant_id = b.tenant_id AND a.bay = b.bay AND a.id < b.id "
                "AND a.status = 'confirmed' AND b.status = 'confirmed' "
                "AND tstzrange(a.starts_at, a.ends_at, '[)') "
                "&& tstzrange(b.starts_at, b.ends_at, '[)') "
                "WHERE a.tenant_id = :t"
            ),
            {"t": str(tenant_id)},
        ).scalar_one()
        over_capacity = conn.execute(
            text(
                "SELECT count(*) FROM (SELECT starts_at FROM appointments WHERE tenant_id = :t "
                "AND status = 'confirmed' GROUP BY starts_at HAVING count(*) > :bays) s"
            ),
            {"t": str(tenant_id), "bays": BAYS},
        ).scalar_one()
        callbacks = conn.execute(
            text("SELECT count(*) FROM callback_requests WHERE tenant_id = :t"),
            {"t": str(tenant_id)},
        ).scalar_one()
    engine.dispose()
    return {
        "confirmed": int(confirmed),
        "bay_overlaps": int(overlaps),
        "slots_over_capacity": int(over_capacity),
        "callbacks": int(callbacks),
    }


def cleanup(settings: Settings, tenant_id: uuid.UUID, vendor_ids: list[str]) -> int:
    """Remove the tenant (rows cascade), its queued jobs and its call state."""
    engine = _engine(settings)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
    engine.dispose()
    r = redis.Redis.from_url(settings.redis_url)
    removed = 0
    for queue in (POST_CALL_QUEUE, CONFIRMATIONS_QUEUE, CRM_QUEUE):
        for raw in r.lrange(queue, 0, -1):
            if str(tenant_id) in raw.decode():
                removed += r.lrem(queue, 1, raw)
    for vendor_id in vendor_ids:
        r.delete(f"call:{vendor_id}")
    r.close()
    return removed


# ── the callers ─────────────────────────────────────────────────────────


@dataclass
class Results:
    latency: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    booked: int = 0
    taken: int = 0


def contended_slots(count: int) -> list[str]:
    """`count` hourly slots from three days out, 12 a day between 8 am and
    7 pm local: the pool every caller competes for. Capacity is count x BAYS."""
    first: date = datetime.now(ZoneInfo(TZ)).date() + timedelta(days=3)
    return [
        f"{(first + timedelta(days=i // 12)).isoformat()}T{8 + i % 12:02d}:00" for i in range(count)
    ]


async def one_call(
    client: httpx.AsyncClient, secret: str, number: str, n: int, slots: list[str], out: Results
) -> str:
    vendor_id = f"lt-{uuid.uuid4()}"
    caller = f"+1720{random.randint(2000000, 9999999)}"
    base = {
        "call": {"id": vendor_id, "customer": {"number": caller}},
        "phoneNumber": {"number": number},
    }
    headers = {"x-vapi-secret": secret, "content-type": "application/json"}

    async def post(step: str, message: dict) -> tuple[dict | None, float]:
        body = json.dumps({"message": {**message, **base}})
        started = time.perf_counter()
        try:
            r = await client.post("/webhooks/voice/vapi", content=body, headers=headers)
        except httpx.HTTPError as exc:
            out.errors[step].append(type(exc).__name__)
            return None, 0.0
        ms = (time.perf_counter() - started) * 1000
        out.latency[step].append(ms)
        if r.status_code != 200:
            out.errors[step].append(f"HTTP {r.status_code}")
            return None, ms
        return r.json(), ms

    async def tool(step: str, name: str, arguments: dict) -> tuple[str, float]:
        reply, ms = await post(
            step,
            {
                "type": "tool-calls",
                "toolCallList": [{"id": f"tc-{step}", "name": name, "arguments": arguments}],
            },
        )
        result = reply["results"][0]["result"] if reply else ""
        if reply and any(m in result.lower() for m in FAILURE_MARKERS):
            out.errors[step].append(result[:60])
        return result, ms

    slot = random.choice(slots)
    await post("call_started", {"type": "status-update", "status": "in-progress"})
    await tool(
        "check_availability", "check_availability", {"service": SERVICE, "preferred_time": slot}
    )
    await tool(
        "record_qualification",
        "record_qualification_answer",
        {"key": "vehicle", "value": "2019 Toyota RAV4"},
    )
    result, booking_ms = await tool(
        "book_appointment",
        "book_appointment",
        {
            "service": SERVICE,
            "starts_at": slot,
            "customer_name": f"Caller {n}",
            "vehicle": "2019 Toyota RAV4",
        },
    )
    # Two different paths: a booking takes the first free bay; a refusal has
    # tried every bay and then searched for alternatives.
    if result.startswith("You're all set"):
        out.booked += 1
        out.latency["  of which booked"].append(booking_ms)
    elif "just taken" in result:
        out.taken += 1
        out.latency["  of which refused (just taken)"].append(booking_ms)
    elif result:
        # Anything else means the booking path didn't do its job.
        out.errors["book_appointment"].append(f"unexpected reply: {result[:50]}")
    if n % 5 == 0:
        await tool(
            "request_callback",
            "request_callback",
            {"reason": "Load test callback", "customer_name": f"Caller {n}"},
        )
    await post(
        "end_of_call",
        {
            "type": "end-of-call-report",
            "endedReason": "customer-ended-call",
            "artifact": {"transcript": "AI: Hello.\nUser: Load test.", "messages": []},
        },
    )
    return vendor_id


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


async def run(args: argparse.Namespace, number: str) -> tuple[Results, float, list[str]]:
    out = Results()
    slots = contended_slots(args.slots)
    sem = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(base_url=args.api, timeout=30, limits=limits) as client:

        async def guarded(n: int) -> str:
            async with sem:
                return await one_call(client, args.secret, number, n, slots, out)

        started = time.perf_counter()
        vendor_ids = await asyncio.gather(*(guarded(n) for n in range(1, args.calls + 1)))
        elapsed = time.perf_counter() - started
    return out, elapsed, list(vendor_ids)


def report(args, out: Results, elapsed: float, checks: dict[str, int]) -> str:
    requests = sum(len(v) for k, v in out.latency.items() if not k.startswith(" "))
    errors = sum(len(v) for v in out.errors.values())
    capacity = args.slots * BAYS
    error_detail = "".join(
        f"\n  - {step}: {dict(Counter(kinds))}" for step, kinds in out.errors.items() if kinds
    )
    lines = [
        f"## Load test: {args.calls} calls, {args.concurrency} concurrent {args.label}".rstrip(),
        "",
        f"- Wall time {elapsed:.1f} s; {requests} webhook requests ({requests / elapsed:.0f}/s); "
        f"{args.calls / elapsed:.1f} calls/s",
        f"- Errors: {errors}{error_detail}",
        "",
        "| Step | n | p50 ms | p95 ms | p99 ms | max ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for step in STEPS:
        values = out.latency.get(step, [])
        if values:
            lines.append(
                f"| {step} | {len(values)} | {statistics.median(values):.0f} "
                f"| {pct(values, 0.95):.0f} | {pct(values, 0.99):.0f} | {max(values):.0f} |"
            )
    lines += [
        "",
        f"Booking under contention: {args.calls} callers for {args.slots} slots x {BAYS} bays "
        f"= {capacity} places.",
        f'- Told "all set": {out.booked}; told "just taken": {out.taken}',
        f"- Confirmed in the database: {checks['confirmed']} "
        "(must equal the callers told 'all set'; random slot choice can leave places unfilled)",
        f"- Bay overlaps: {checks['bay_overlaps']}; "
        f"slots over capacity: {checks['slots_over_capacity']}",
        f"- Callbacks saved: {checks['callbacks']} (expected {args.calls // 5})",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default=os.environ.get("LOADTEST_API", "http://localhost:8000"))
    parser.add_argument("--secret", default=os.environ.get("VAPI_WEBHOOK_SECRET", ""))
    parser.add_argument("--calls", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--slots", type=int, default=8, help="contended slots (x3 bays)")
    parser.add_argument(
        "--label", default="", help="text for the report heading, e.g. '(4 workers)'"
    )
    parser.add_argument("--keep", action="store_true", help="keep the tenant for inspection")
    args = parser.parse_args()
    if not args.secret:
        parser.error("set VAPI_WEBHOOK_SECRET (the API's value) or pass --secret")

    settings = Settings()
    tenant_id, number = create_tenant(settings)
    vendor_ids: list[str] = []
    try:
        out, elapsed, vendor_ids = asyncio.run(run(args, number))
        checks = integrity(settings, tenant_id)
        print(report(args, out, elapsed, checks))
        sound = (
            checks["bay_overlaps"] == 0
            and checks["slots_over_capacity"] == 0
            and checks["confirmed"] == out.booked
        )
        return 0 if sound and not any(out.errors.values()) else 1
    finally:
        if not args.keep:
            removed = cleanup(settings, tenant_id, vendor_ids)
            print(f"\n(cleaned up the load-test tenant and {removed} queued jobs)")


if __name__ == "__main__":
    raise SystemExit(main())
