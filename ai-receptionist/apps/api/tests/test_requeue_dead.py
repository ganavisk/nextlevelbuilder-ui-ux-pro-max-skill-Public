"""scripts/requeue_dead.py: dead jobs go back with a fresh retry budget,
exactly once, and only the ones asked for."""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
import redis as sync_redis

from app.core.config import Settings
from app.core.queue import dead_letter_key
from tests.conftest import requires_services

_spec = importlib.util.spec_from_file_location(
    "requeue_dead", Path(__file__).resolve().parents[1] / "scripts" / "requeue_dead.py"
)
requeue_dead = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(requeue_dead)  # type: ignore[union-attr]

QUEUE = "voice_sync"  # rarely busy; every job here carries a unique tenant id


@pytest.fixture
def r():
    client = sync_redis.Redis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    tenant = str(uuid.uuid4())
    yield client, tenant
    for key in (f"queue:{QUEUE}", dead_letter_key(f"queue:{QUEUE}")):
        for raw in client.lrange(key, 0, -1):
            if tenant in raw.decode():
                client.lrem(key, 0, raw)
    client.close()


def _bury(client, tenant: str, job_type: str = "voice_sync", n: int = 1) -> None:
    for i in range(n):
        client.rpush(
            dead_letter_key(f"queue:{QUEUE}"),
            json.dumps(
                {
                    "type": job_type,
                    "tenant_id": tenant,
                    "n": i,
                    "attempts": 5,
                    "last_error": "HTTPStatusError: 503",
                    "dead_reason": "max_attempts",
                    "died_at": 1.0,
                }
            ),
        )


def _ours(client, key: str, tenant: str) -> list[dict]:
    return [j for j in map(json.loads, client.lrange(key, 0, -1)) if j.get("tenant_id") == tenant]


@requires_services
def test_requeued_jobs_get_a_fresh_retry_budget_and_lose_their_failure_details(r) -> None:
    client, tenant = r
    _bury(client, tenant, n=2)
    moved, reasons = requeue_dead.requeue(client, QUEUE, tenant=tenant)
    assert moved == 2
    assert reasons == {"voice_sync: max_attempts / HTTPStatusError: 503": 2}
    back = _ours(client, f"queue:{QUEUE}", tenant)
    assert [j["n"] for j in back] == [0, 1]
    assert all(
        j["attempts"] == 0 and "dead_reason" not in j and "last_error" not in j for j in back
    )
    assert _ours(client, dead_letter_key(f"queue:{QUEUE}"), tenant) == []


@requires_services
def test_a_dry_run_moves_nothing(r) -> None:
    client, tenant = r
    _bury(client, tenant)
    assert requeue_dead.requeue(client, QUEUE, tenant=tenant, dry_run=True)[0] == 1
    assert _ours(client, f"queue:{QUEUE}", tenant) == []


@requires_services
def test_only_the_asked_for_jobs_move(r) -> None:
    client, tenant = r
    _bury(client, tenant, job_type="voice_sync")
    _bury(client, tenant, job_type="something_else")
    moved, _ = requeue_dead.requeue(client, QUEUE, tenant=tenant, job_type="voice_sync")
    assert moved == 1
    left = _ours(client, dead_letter_key(f"queue:{QUEUE}"), tenant)
    assert [j["type"] for j in left] == ["something_else"]


@requires_services
def test_a_job_is_requeued_once_even_if_two_runs_overlap(r) -> None:
    """The second run saw the job too, but the first already moved it."""
    client, tenant = r
    _bury(client, tenant)
    raw = client.lrange(dead_letter_key(f"queue:{QUEUE}"), 0, -1)[-1]
    job = json.loads(raw)
    requeue_dead.requeue(client, QUEUE, tenant=tenant)
    move = client.register_script(requeue_dead._MOVE)
    again = move(
        keys=[dead_letter_key(f"queue:{QUEUE}"), f"queue:{QUEUE}"],
        args=[raw, json.dumps(requeue_dead.revived(job))],
    )
    assert again == 0
    assert len(_ours(client, f"queue:{QUEUE}", tenant)) == 1
