"""Job queue reliability, by fault injection (milestone M4's criterion).

These run against real Redis with a unique queue per test. The failures are
injected rather than simulated with mocks, because what is being tested is
the interaction between the handler raising, the Redis key layout, and the
retry bookkeeping — a mocked Redis would only assert that the code calls the
functions it calls.
"""

import asyncio
import json
import uuid

import pytest
import redis.asyncio as aioredis

from app.core.config import Settings
from app.core.errors import IntegrationError, PermanentIntegrationError
from app.core.queue import (
    backoff_seconds,
    dead_letter_key,
    delayed_key,
    enqueue,
    processing_key,
    promote_due_jobs,
    reclaim_orphans,
    run_worker,
    worker_redis,
)
from tests.conftest import requires_services

FAR_FUTURE = 10_000_000_000.0


@pytest.fixture
async def redis():
    settings = Settings(_env_file=None, app_env="test")
    client = aioredis.from_url(settings.redis_url)
    yield client
    await client.aclose()


@pytest.fixture
def queue():
    return f"queue:test:{uuid.uuid4().hex[:8]}"


async def _drain(redis, queue: str) -> None:
    await redis.delete(queue, processing_key(queue), delayed_key(queue), dead_letter_key(queue))


async def _dead_letters(redis, queue: str) -> list[dict]:
    return [json.loads(r) for r in await redis.lrange(dead_letter_key(queue), 0, -1)]


# ── the happy path still works ─────────────────────────────────────────────


@requires_services
async def test_successful_job_leaves_no_residue(redis, queue) -> None:
    """A completed job must not linger in `processing`, or reclaim would
    re-run it on the next restart."""
    seen = []

    async def handler(job):
        seen.append(job)

    await enqueue(redis, queue, {"type": "t", "n": 1})
    await run_worker(redis, queue, handler, block_seconds=1, max_jobs=1)

    assert len(seen) == 1
    assert await redis.llen(processing_key(queue)) == 0
    assert await redis.llen(queue) == 0
    assert await redis.zcard(delayed_key(queue)) == 0
    assert await _dead_letters(redis, queue) == []
    await _drain(redis, queue)


# ── fault injection: transient failure ─────────────────────────────────────


@requires_services
async def test_transient_failure_is_retried_and_then_succeeds(redis, queue) -> None:
    """The headline behaviour: a vendor blip must not lose the job."""
    attempts = []

    async def flaky(job):
        attempts.append(job.get("attempts", 0))
        if len(attempts) < 3:
            raise IntegrationError("HubSpot 503")

    await enqueue(redis, queue, {"type": "crm_sync", "call_id": "abc"})

    # Each pass: one attempt, then the retry waits on backoff. Promoting with
    # a future clock is how the test skips the wait without sleeping.
    for _ in range(3):
        await run_worker(redis, queue, flaky, block_seconds=1, max_jobs=1)
        await promote_due_jobs(redis, queue, now=FAR_FUTURE)

    assert attempts == [0, 1, 2], "attempt counter must survive each retry"
    assert await _dead_letters(redis, queue) == []
    assert await redis.llen(processing_key(queue)) == 0
    await _drain(redis, queue)


@requires_services
async def test_retry_carries_the_error_forward_for_diagnosis(redis, queue) -> None:
    seen = []

    async def failing(job):
        seen.append(job)
        raise IntegrationError("HubSpot 503 service unavailable")

    await enqueue(redis, queue, {"type": "crm_sync"})
    await run_worker(redis, queue, failing, block_seconds=1, max_jobs=1)
    await promote_due_jobs(redis, queue, now=FAR_FUTURE)
    await run_worker(redis, queue, failing, block_seconds=1, max_jobs=1)

    assert "503" in seen[1]["last_error"]
    assert seen[1]["attempts"] == 1
    await _drain(redis, queue)


@requires_services
async def test_exhausted_retries_dead_letter_with_the_reason(redis, queue) -> None:
    async def always_fails(job):
        raise IntegrationError("HubSpot is down")

    await enqueue(redis, queue, {"type": "crm_sync", "call_id": "abc"})
    for _ in range(3):
        await run_worker(redis, queue, always_fails, block_seconds=1, max_jobs=1, max_attempts=3)
        await promote_due_jobs(redis, queue, now=FAR_FUTURE)

    dead = await _dead_letters(redis, queue)
    assert len(dead) == 1
    assert dead[0]["dead_reason"] == "max_attempts"
    assert dead[0]["attempts"] == 3
    assert dead[0]["call_id"] == "abc", "the payload must survive to the dead letter"
    # Nothing left anywhere else — the job is accounted for exactly once.
    assert await redis.llen(queue) == 0
    assert await redis.llen(processing_key(queue)) == 0
    assert await redis.zcard(delayed_key(queue)) == 0
    await _drain(redis, queue)


# ── fault injection: permanent failure ─────────────────────────────────────


@requires_services
async def test_permanent_failure_skips_retries_entirely(redis, queue) -> None:
    """A 400 will still be a 400 in four minutes. Retrying it just delays the
    alert and hammers a vendor that already said no."""
    calls = []

    async def rejected(job):
        calls.append(job)
        raise PermanentIntegrationError("HubSpot 400: property does not exist")

    await enqueue(redis, queue, {"type": "crm_sync"})
    await run_worker(redis, queue, rejected, block_seconds=1, max_jobs=1)

    assert len(calls) == 1
    dead = await _dead_letters(redis, queue)
    assert len(dead) == 1
    assert dead[0]["dead_reason"] == "permanent"
    assert dead[0]["attempts"] == 1, "a permanent failure must not burn the retry budget"
    assert await redis.zcard(delayed_key(queue)) == 0, "nothing should be waiting on backoff"
    await _drain(redis, queue)


# ── fault injection: worker crash ──────────────────────────────────────────


@requires_services
async def test_job_survives_a_worker_crash_mid_flight(redis, queue) -> None:
    """Phase 1: "a failed CRM sync must never drop a call record". A worker
    killed while holding a job must leave it recoverable, not lost."""
    started = asyncio.Event()

    async def hangs(job):
        started.set()
        await asyncio.sleep(60)  # stands in for a worker that never returns

    task = asyncio.create_task(run_worker(redis, queue, hangs, block_seconds=1, max_jobs=1))
    await enqueue(redis, queue, {"type": "crm_sync", "call_id": "abc"})
    await asyncio.wait_for(started.wait(), timeout=5)

    # Kill the worker mid-job, the way SIGKILL or a container eviction would.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The payload is in `processing`, not gone.
    assert await redis.llen(processing_key(queue)) == 1
    assert await redis.llen(queue) == 0

    # A restart recovers it.
    assert await reclaim_orphans(redis, queue) == 1
    assert await redis.llen(queue) == 1
    recovered = json.loads(await redis.lindex(queue, 0))
    assert recovered["call_id"] == "abc"
    await _drain(redis, queue)


@requires_services
async def test_reclaim_is_a_no_op_when_nothing_is_stranded(redis, queue) -> None:
    assert await reclaim_orphans(redis, queue) == 0
    await _drain(redis, queue)


# ── backoff and scheduling ─────────────────────────────────────────────────


def test_backoff_grows_and_is_capped() -> None:
    # Jittered, so compare ranges rather than exact values.
    assert 1 <= backoff_seconds(1) <= 2
    assert 2 <= backoff_seconds(2) <= 4
    assert 4 <= backoff_seconds(3) <= 8
    # Far beyond the cap, still bounded.
    assert backoff_seconds(20) <= 300


def test_backoff_is_jittered() -> None:
    """Un-jittered backoff retries every job of a mass failure at the same
    instant, which is how a recovering vendor gets knocked over again."""
    samples = {backoff_seconds(4) for _ in range(50)}
    assert len(samples) > 1


@requires_services
async def test_retry_is_not_visible_until_its_backoff_elapses(redis, queue) -> None:
    async def fails(job):
        raise IntegrationError("nope")

    await enqueue(redis, queue, {"type": "t"})
    await run_worker(redis, queue, fails, block_seconds=1, max_jobs=1)

    assert await redis.zcard(delayed_key(queue)) == 1
    assert await redis.llen(queue) == 0, "a job on backoff must not be immediately re-runnable"

    assert await promote_due_jobs(redis, queue, now=0) == 0, "not due yet"
    assert await promote_due_jobs(redis, queue, now=FAR_FUTURE) == 1
    assert await redis.llen(queue) == 1
    await _drain(redis, queue)


@requires_services
async def test_concurrent_promotion_does_not_duplicate_a_job(redis, queue) -> None:
    """Two workers promoting the same due retry must produce one job, not
    two — otherwise every retry doubles the work with each extra worker."""

    async def fails(job):
        raise IntegrationError("nope")

    await enqueue(redis, queue, {"type": "t"})
    await run_worker(redis, queue, fails, block_seconds=1, max_jobs=1)

    promoted = await asyncio.gather(
        *(promote_due_jobs(redis, queue, now=FAR_FUTURE) for _ in range(5))
    )
    assert sum(promoted) == 1
    assert await redis.llen(queue) == 1
    await _drain(redis, queue)


# ── malformed payloads ─────────────────────────────────────────────────────


@requires_services
async def test_unparseable_payload_is_dead_lettered_not_crash_looped(redis, queue) -> None:
    """A corrupt entry must not wedge the queue: without this the worker
    would fail to parse, never consume it, and stop processing everything
    behind it."""
    called = []

    async def handler(job):
        called.append(job)

    await redis.rpush(queue, "{not json")
    await enqueue(redis, queue, {"type": "good"})
    await run_worker(redis, queue, handler, block_seconds=1, max_jobs=2)

    assert [j["type"] for j in called] == ["good"], "the good job behind it must still run"
    assert await redis.llen(dead_letter_key(queue)) == 1
    assert await redis.llen(processing_key(queue)) == 0
    await _drain(redis, queue)


@requires_services
async def test_dead_letter_list_is_bounded(redis, queue) -> None:
    """A sustained outage must not grow an unbounded Redis list."""
    from app.core.queue import MAX_DEAD_LETTERS, dead_letter

    for n in range(5):
        await dead_letter(redis, queue, {"type": "t", "n": n}, reason="test")
    assert await redis.llen(dead_letter_key(queue)) == 5
    assert MAX_DEAD_LETTERS > 0
    await _drain(redis, queue)


# ── client timeout vs. blocking pop ──────────────────────────────────────


@requires_services
async def test_idle_worker_survives_a_client_timeout_shorter_than_the_block_window() -> None:
    """redis-py 8 defaults its socket timeout to 5s — exactly the worker's
    5s blocking-pop window — so an idle worker used to crash with a Redis
    TimeoutError (found running the production image). A mis-sized client must
    read as "no job yet", not as a failure."""
    settings = Settings(_env_file=None, app_env="test")
    redis = aioredis.from_url(settings.redis_url, socket_timeout=1)
    queue = f"queue:test-idle-{uuid.uuid4().hex[:8]}"
    task = asyncio.create_task(
        run_worker(redis, queue, _never_called, block_seconds=2)  # 2s block, 1s client timeout
    )
    try:
        await asyncio.sleep(3.5)  # long enough for at least one client-side timeout
        assert not task.done(), "the idle worker exited instead of polling again"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await redis.aclose()


@requires_services
async def test_worker_redis_client_outlasts_the_blocking_pop() -> None:
    settings = Settings(_env_file=None, app_env="test")
    redis = worker_redis(settings.redis_url, block_seconds=2)
    try:
        timeout = redis.connection_pool.connection_kwargs["socket_timeout"]
        assert timeout > 2
        # And it really can sit in a blocking pop for the whole window.
        queue = f"queue:test-block-{uuid.uuid4().hex[:8]}"
        assert await redis.blmove(queue, f"{queue}:processing", timeout=2) is None
    finally:
        await redis.aclose()


async def _never_called(job):  # pragma: no cover
    raise AssertionError("no job should be delivered")


# ── graceful stop ────────────────────────────────────────────────────────


@requires_services
async def test_stop_signal_finishes_the_job_in_hand_and_leaves_the_rest_queued() -> None:
    settings = Settings(_env_file=None, app_env="test")
    redis = worker_redis(settings.redis_url, block_seconds=1)
    queue = f"queue:test-stop-{uuid.uuid4().hex[:8]}"
    stop = asyncio.Event()
    handled: list[str] = []

    async def handler(job: dict) -> None:
        handled.append(job["name"])
        stop.set()  # the stop request arrives while this job is running
        await asyncio.sleep(0.2)  # ...and the job still has work left to do

    try:
        await enqueue(redis, queue, {"type": "t", "name": "first"})
        await enqueue(redis, queue, {"type": "t", "name": "second"})
        await asyncio.wait_for(run_worker(redis, queue, handler, block_seconds=1, stop=stop), 5)

        assert handled == ["first"], "the job in hand must finish; the next must not start"
        assert await redis.llen(processing_key(queue)) == 0, "finished job must not be left behind"
        assert await redis.llen(queue) == 1, "the unstarted job stays queued for the next worker"
    finally:
        await redis.delete(queue, processing_key(queue))
        await redis.aclose()


@requires_services
async def test_an_idle_worker_exits_promptly_once_stopped() -> None:
    settings = Settings(_env_file=None, app_env="test")
    redis = worker_redis(settings.redis_url, block_seconds=1)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(
            redis,
            f"queue:test-idle-stop-{uuid.uuid4().hex[:8]}",
            _never_called,
            block_seconds=1,
            stop=stop,
        )
    )
    try:
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=3)  # within one block window
    finally:
        await redis.aclose()
