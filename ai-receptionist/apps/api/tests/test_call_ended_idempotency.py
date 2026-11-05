"""A vendor retries a webhook when it doesn't get a 2xx, and a 2xx can be lost
too — so the end-of-call report must be safe to deliver more than once, and a
failure *after* the database commit must not strand the call forever.

Both used to be broken: the second delivery violated `transcripts.call_id`
(500), and a failed enqueue followed by the vendor's retry never queued the
post-call job, so the call was never summarized or synced to the CRM.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import app.modules.telephony.service as telephony_service
from app.core.config import Settings
from app.core.queue import POST_CALL_QUEUE
from app.main import create_app
from tests.conftest import requires_services

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-secret"


def _body(name: str, number: str, call_id: str) -> str:
    raw = (FIXTURES / name).read_text()
    raw = raw.replace("+15550100001", number).replace("vapi-call-0001", call_id)
    return json.dumps(json.loads(raw))


@requires_services
def test_redelivered_end_of_call_report_is_accepted_and_not_duplicated() -> None:
    settings = Settings(_env_file=None, app_env="test", vapi_webhook_secret=SECRET)
    engine = create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )
    redis = sync_redis.Redis.from_url(settings.redis_url)
    tenant_id = uuid.uuid4()
    number = f"+1997{uuid.uuid4().int % 10**7:07d}"
    vendor_call_id = f"redeliver-{uuid.uuid4().hex[:10]}"

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name, vertical) VALUES (:i, :s, 'T', 'dental')"),
            {"i": str(tenant_id), "s": f"redeliver-{tenant_id.hex[:8]}"},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO phone_numbers (tenant_id, e164) VALUES (:t, :n)"),
            {"t": str(tenant_id), "n": number},
        )
    try:
        with TestClient(create_app(settings)) as client:

            def post(fixture: str):
                return client.post(
                    "/webhooks/voice/vapi",
                    content=_body(fixture, number, vendor_call_id),
                    headers={"x-vapi-secret": SECRET},
                )

            assert post("vapi_call_started.json").status_code == 200
            assert post("vapi_end_of_call_report.json").status_code == 200
            first_ended_at = _ended_at(engine, tenant_id, vendor_call_id)

            assert post("vapi_end_of_call_report.json").status_code == 200

        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            transcripts = conn.execute(
                text(
                    "SELECT count(*) FROM transcripts t JOIN calls c ON c.id = t.call_id "
                    "WHERE c.vendor_call_id = :v"
                ),
                {"v": vendor_call_id},
            ).scalar_one()
        assert transcripts == 1
        # A redelivery must not move the call's end time forward.
        assert _ended_at(engine, tenant_id, vendor_call_id) == first_ended_at
    finally:
        _cleanup(engine, redis, tenant_id)


@requires_services
def test_vendor_retry_after_failed_enqueue_still_queues_the_post_call_job() -> None:
    settings = Settings(_env_file=None, app_env="test", vapi_webhook_secret=SECRET)
    engine = create_engine(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    )
    redis = sync_redis.Redis.from_url(settings.redis_url)
    tenant_id = uuid.uuid4()
    number = f"+1996{uuid.uuid4().int % 10**7:07d}"
    vendor_call_id = f"enqueue-fail-{uuid.uuid4().hex[:10]}"

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name, vertical) VALUES (:i, :s, 'T', 'dental')"),
            {"i": str(tenant_id), "s": f"enqfail-{tenant_id.hex[:8]}"},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO phone_numbers (tenant_id, e164) VALUES (:t, :n)"),
            {"t": str(tenant_id), "n": number},
        )

    async def redis_is_down(*args, **kwargs):
        raise ConnectionError("simulated Redis outage")

    def jobs_for_tenant() -> list[dict]:
        return [
            j
            for j in (json.loads(x) for x in redis.lrange(POST_CALL_QUEUE, 0, -1))
            if j.get("tenant_id") == str(tenant_id)
        ]

    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:

            def post(fixture: str):
                return client.post(
                    "/webhooks/voice/vapi",
                    content=_body(fixture, number, vendor_call_id),
                    headers={"x-vapi-secret": SECRET},
                )

            assert post("vapi_call_started.json").status_code == 200
            with patch.object(telephony_service, "enqueue", redis_is_down):
                assert post("vapi_end_of_call_report.json").status_code == 500
            assert jobs_for_tenant() == []  # the database committed; the queue did not

            assert post("vapi_end_of_call_report.json").status_code == 200
        assert len(jobs_for_tenant()) == 1
    finally:
        _cleanup(engine, redis, tenant_id)


def _ended_at(engine, tenant_id, vendor_call_id):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return conn.execute(
            text("SELECT ended_at FROM calls WHERE vendor_call_id = :v"), {"v": vendor_call_id}
        ).scalar_one()


def _cleanup(engine, redis, tenant_id) -> None:
    for item in redis.lrange(POST_CALL_QUEUE, 0, -1):
        if str(tenant_id) in item.decode():
            redis.lrem(POST_CALL_QUEUE, 0, item)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": str(tenant_id)})
    engine.dispose()
    redis.close()
