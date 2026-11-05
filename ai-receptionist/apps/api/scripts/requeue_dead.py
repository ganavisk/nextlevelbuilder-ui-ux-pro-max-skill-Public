"""Inspect and requeue dead-lettered jobs.

A job is dead-lettered after its last retry fails, or at once on a permanent
error (e.g. a revoked HubSpot grant). Once the cause is fixed, requeue it
with a fresh retry budget. Every handler is idempotent, so a requeued job
never double-sends a text or duplicates a CRM deal.

    python scripts/requeue_dead.py crm                    # list: counts and reasons
    python scripts/requeue_dead.py crm --requeue --dry-run
    python scripts/requeue_dead.py crm --requeue --tenant <tenant-id>
    python scripts/requeue_dead.py confirmations --requeue --type appointment_confirmation

Queues: post_call, confirmations, crm, voice_sync. Uses REDIS_URL.
"""

import argparse
import json
from collections import Counter
from typing import Any

import redis

from app.core.config import Settings
from app.core.queue import dead_letter_key

QUEUES = ("post_call", "confirmations", "crm", "voice_sync")
DEAD_FIELDS = ("dead_reason", "died_at", "last_error")


def revived(job: dict[str, Any]) -> dict[str, Any]:
    """The job as first enqueued: failure details stripped, retries reset."""
    clean = {k: v for k, v in job.items() if k not in DEAD_FIELDS}
    clean["attempts"] = 0
    return clean


def matches(job: dict[str, Any], tenant: str | None, job_type: str | None) -> bool:
    return (tenant is None or job.get("tenant_id") == tenant) and (
        job_type is None or job.get("type") == job_type
    )


# Push only if this run's remove succeeded, atomically: two operators running
# this at once can't both requeue the same job.
_MOVE = """
local n = redis.call('LREM', KEYS[1], 1, ARGV[1])
if n == 1 then redis.call('RPUSH', KEYS[2], ARGV[2]) end
return n
"""


def requeue(
    r: "redis.Redis[bytes]",
    queue_name: str,
    *,
    tenant: str | None = None,
    job_type: str | None = None,
    dry_run: bool = False,
) -> tuple[int, Counter[str]]:
    """(jobs requeued, or that would be; their failure reasons)."""
    queue = f"queue:{queue_name}"
    dead = dead_letter_key(queue)
    chosen = [
        (raw, job)
        for raw, job in ((raw, json.loads(raw)) for raw in r.lrange(dead, 0, -1))
        if matches(job, tenant, job_type)
    ]
    reasons = Counter(
        f"{job.get('type')}: {job.get('dead_reason')} / {str(job.get('last_error', ''))[:80]}"
        for _, job in chosen
    )
    if dry_run:
        return len(chosen), reasons
    move = r.register_script(_MOVE)
    moved = sum(
        int(move(keys=[dead, queue], args=[raw, json.dumps(revived(job))])) for raw, job in chosen
    )
    return moved, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("queue", choices=QUEUES)
    parser.add_argument("--requeue", action="store_true", help="move matching jobs back")
    parser.add_argument("--dry-run", action="store_true", help="with --requeue: only report")
    parser.add_argument("--tenant", help="only this tenant id")
    parser.add_argument("--type", dest="job_type", help="only this job type")
    args = parser.parse_args()

    r = redis.Redis.from_url(Settings().redis_url)
    dead = dead_letter_key(f"queue:{args.queue}")
    print(f"{dead}: {r.llen(dead)} dead")
    count, reasons = requeue(
        r,
        args.queue,
        tenant=args.tenant,
        job_type=args.job_type,
        dry_run=not args.requeue or args.dry_run,
    )
    print(f"{count} matching")
    for reason, n in reasons.most_common():
        print(f"  {n:>4}  {reason}")
    if args.requeue:
        verb = "would requeue" if args.dry_run else "requeued"
        print(f"{verb} {count} onto queue:{args.queue}")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
