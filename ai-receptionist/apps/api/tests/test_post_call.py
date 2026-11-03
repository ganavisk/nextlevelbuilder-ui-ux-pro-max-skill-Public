"""Post-call worker against real Postgres + Redis.

The property under test is the handoff to the CRM queue. Classification is
committed first and the CRM job enqueued second, so a crash or Redis blip in
between leaves a classified call with no CRM job. The queue then retries the
job — and the retry used to see "already classified" and return without ever
enqueuing the CRM sync.
"""

import json
import uuid
from unittest.mock import patch

import pytest
import redis.asyncio as aioredis
from sqlalchemy import create_engine, text

import app.workers.post_call as post_call
from app.core.config import Settings
from app.core.queue import CRM_QUEUE
from tests.conftest import requires_services
from tests.fakes import FakeLLMProvider

CLASSIFICATION = json.dumps(
    {"summary": "Booked a cleaning.", "outcome": "appointment_booked", "sentiment": "positive"}
)


@pytest.fixture
def classified_call_setup():
    """A tenant with one ended, unclassified call and a two-turn transcript.
    Yields (tenant_id, call_id)."""
    settings = Settings(_env_file=None, app_env="test")
    engine = create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )
    tenant_id, call_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name, vertical) VALUES (:i, :s, 'T', 'dental')"),
            {"i": str(tenant_id), "s": f"postcall-{tenant_id.hex[:8]}"},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO calls (id, tenant_id, vendor_call_id, caller_e164, ended_at) "
                "VALUES (:c, :t, :v, '+15550001111', now())"
            ),
            {"c": str(call_id), "t": str(tenant_id), "v": f"pc-{call_id.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO transcripts (tenant_id, call_id, turns) "
                "VALUES (:t, :c, CAST(:x AS jsonb))"
            ),
            {
                "t": str(tenant_id),
                "c": str(call_id),
                "x": json.dumps(
                    [
                        {"role": "caller", "text": "I'd like a cleaning."},
                        {"role": "assistant", "text": "Booked for Tuesday."},
                    ]
                ),
            },
        )
    try:
        yield tenant_id, call_id
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": str(tenant_id)})
        engine.dispose()


async def _crm_jobs(redis: aioredis.Redis, call_id: uuid.UUID) -> list[dict]:
    return [
        j
        for j in (json.loads(x) for x in await redis.lrange(CRM_QUEUE, 0, -1))
        if j.get("call_id") == str(call_id)
    ]


async def _drop_crm_jobs(redis: aioredis.Redis, call_id: uuid.UUID) -> None:
    for item in await redis.lrange(CRM_QUEUE, 0, -1):
        if str(call_id) in item.decode():
            await redis.lrem(CRM_QUEUE, 0, item)


@requires_services
async def test_classifies_the_call_and_enqueues_one_crm_job(classified_call_setup) -> None:
    tenant_id, call_id = classified_call_setup
    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    llm = FakeLLMProvider(CLASSIFICATION)
    try:
        await post_call.handle_post_call(
            {"tenant_id": str(tenant_id), "call_id": str(call_id)}, llm=llm, redis=redis
        )
        jobs = await _crm_jobs(redis, call_id)
        assert len(jobs) == 1
        assert jobs[0]["outcome"] == "appointment_booked"
        assert jobs[0]["summary"] == "Booked a cleaning."
    finally:
        await _drop_crm_jobs(redis, call_id)
        await redis.aclose()


@requires_services
async def test_retry_after_a_failed_crm_enqueue_still_hands_off_without_reclassifying(
    classified_call_setup,
) -> None:
    """The regression: classification committed, CRM enqueue blew up, the
    queue retried the job — which must now complete the handoff, using the
    stored classification rather than paying for another LLM call."""
    tenant_id, call_id = classified_call_setup
    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    llm = FakeLLMProvider(CLASSIFICATION)
    job = {"tenant_id": str(tenant_id), "call_id": str(call_id)}

    async def redis_blip(*args, **kwargs):
        raise ConnectionError("simulated Redis outage")

    try:
        with patch.object(post_call, "enqueue", redis_blip), pytest.raises(ConnectionError):
            await post_call.handle_post_call(job, llm=llm, redis=redis)
        assert await _crm_jobs(redis, call_id) == []
        assert len(llm.calls) == 1

        await post_call.handle_post_call(job, llm=llm, redis=redis)  # the queue's retry

        jobs = await _crm_jobs(redis, call_id)
        assert len(jobs) == 1
        assert jobs[0]["outcome"] == "appointment_booked"
        assert jobs[0]["summary"] == "Booked a cleaning."
        assert len(llm.calls) == 1, "the retry must not classify the call a second time"
    finally:
        await _drop_crm_jobs(redis, call_id)
        await redis.aclose()


@requires_services
async def test_a_call_that_does_not_exist_is_a_no_op(classified_call_setup) -> None:
    tenant_id, _ = classified_call_setup
    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    missing = uuid.uuid4()
    try:
        await post_call.handle_post_call(
            {"tenant_id": str(tenant_id), "call_id": str(missing)},
            llm=FakeLLMProvider(CLASSIFICATION),
            redis=redis,
        )
        assert await _crm_jobs(redis, missing) == []
    finally:
        await redis.aclose()


@requires_services
async def test_simulated_test_lab_calls_are_classified_but_never_reach_the_crm(
    classified_call_setup,
) -> None:
    tenant_id, call_id = classified_call_setup
    engine = create_engine(
        Settings(_env_file=None, app_env="test").database_url.replace(
            "postgresql+asyncpg://", "postgresql+psycopg2://", 1
        )
    )
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text("UPDATE calls SET direction = 'simulated' WHERE id = :c"), {"c": str(call_id)}
        )
    redis = aioredis.from_url(Settings(_env_file=None, app_env="test").redis_url)
    llm = FakeLLMProvider(CLASSIFICATION)
    job = {"tenant_id": str(tenant_id), "call_id": str(call_id)}
    try:
        await post_call.handle_post_call(job, llm=llm, redis=redis)
        await post_call.handle_post_call(job, llm=llm, redis=redis)  # the re-handoff path too
        assert len(llm.calls) == 1, "the call is still classified once"
        assert await _crm_jobs(redis, call_id) == []
        with engine.begin() as conn:
            scope = text("SELECT set_config('app.tenant_id', :t, true)")
            conn.execute(scope, {"t": str(tenant_id)})
            outcome = conn.execute(
                text("SELECT outcome FROM calls WHERE id = :c"), {"c": str(call_id)}
            ).scalar_one()
        assert outcome == "appointment_booked"
    finally:
        await _drop_crm_jobs(redis, call_id)
        await redis.aclose()
        engine.dispose()
