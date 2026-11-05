"""Integration test: the full M1 call lifecycle through the real stack.

Drives the actual webhook endpoint (signature check included) against a live
Postgres + Redis: call started → progress event → end-of-call report, then
asserts the call row, transcript, and post-call job. Skipped automatically
when the services aren't reachable (unit CI still passes); the pipeline CI
runs it against its service containers.
"""

import json
import uuid
from pathlib import Path

import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.queue import POST_CALL_QUEUE
from app.main import create_app
from tests.conftest import requires_services

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-secret"


def _fixture(name: str, number: str, call_id: str) -> dict:
    raw = (FIXTURES / name).read_text()
    return json.loads(raw.replace("+15550100001", number).replace("vapi-call-0001", call_id))


@requires_services
def test_full_call_lifecycle() -> None:
    settings = Settings(_env_file=None, app_env="test", vapi_webhook_secret=SECRET)
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    redis = sync_redis.Redis.from_url(settings.redis_url)

    tenant_id = uuid.uuid4()
    number = f"+1999{uuid.uuid4().int % 10**7:07d}"
    vendor_call_id = f"itest-{uuid.uuid4().hex[:12]}"

    # Seed a tenant + inbound number (phone_numbers is RLS-scoped).
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) VALUES (:id, :slug, :n, 'dental')"
            ),
            {"id": str(tenant_id), "slug": f"flow-test-{tenant_id.hex[:8]}", "n": "Flow Test"},
        )
        conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        conn.execute(
            text("INSERT INTO phone_numbers (tenant_id, e164) VALUES (:tid, :e164)"),
            {"tid": str(tenant_id), "e164": number},
        )
        # An active agent config is required for the TOOL_CALL path (it
        # supplies the tools and escalation policy).
        conn.execute(
            text(
                "INSERT INTO agent_configs "
                "(tenant_id, version, is_active, system_prompt, first_message) "
                "VALUES (:tid, 1, true, 'You are a receptionist.', 'Hello!')"
            ),
            {"tid": str(tenant_id)},
        )

    try:
        app = create_app(settings)
        with TestClient(app) as client:

            def post(fixture_name: str):
                body = json.dumps(_fixture(fixture_name, number, vendor_call_id))
                return client.post(
                    "/webhooks/voice/vapi", content=body, headers={"x-vapi-secret": SECRET}
                )

            # Unsigned requests are rejected at the edge.
            unsigned = client.post("/webhooks/voice/vapi", content="{}")
            assert unsigned.status_code == 401

            assert post("vapi_call_started.json").json() == {"status": "accepted"}
            # The TOOL_CALL path returns Vapi-shaped tool results, not a
            # status ack. The fixture's tool ("check_availability") is not a
            # real tool, so it hits the safe fallback — still a 200 with a
            # results envelope, which is what proves the path is wired.
            tool_response = post("vapi_tool_calls.json").json()
            assert "results" in tool_response
            assert tool_response["results"][0]["toolCallId"] == "toolcall-0001"
            assert post("vapi_end_of_call_report.json").json() == {"status": "accepted"}

        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
            )
            call = conn.execute(
                text(
                    "SELECT id, ended_at, recording_url, vendor_cost_cents FROM calls "
                    "WHERE vendor_call_id = :vcid"
                ),
                {"vcid": vendor_call_id},
            ).one()
            assert call.ended_at is not None
            assert call.recording_url.endswith(".wav")
            assert call.vendor_cost_cents == 37

            turns = conn.execute(
                text("SELECT turns FROM transcripts WHERE call_id = :cid"), {"cid": call.id}
            ).scalar_one()
            assert [t["role"] for t in turns] == ["assistant", "caller", "assistant"]
            assert "new patients" in turns[1]["text"]

            event_types = {
                row.event_type
                for row in conn.execute(
                    text("SELECT event_type FROM call_events WHERE call_id = :cid"),
                    {"cid": call.id},
                )
            }
            assert {"tool_call", "call_ended"} <= event_types

        # The finished call was handed to the async post-call queue.
        jobs = [json.loads(item) for item in redis.lrange(POST_CALL_QUEUE, 0, -1)]
        ours = [j for j in jobs if j.get("call_id") == str(call.id)]
        assert len(ours) == 1
    finally:
        for item in redis.lrange(POST_CALL_QUEUE, 0, -1):
            if vendor_call_id in item.decode() or str(tenant_id) in item.decode():
                redis.lrem(POST_CALL_QUEUE, 0, item)
        with engine.begin() as conn:
            # FK cascades bypass RLS, so deleting the tenant removes all children.
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()
        redis.close()


@requires_services
def test_a_call_whose_start_event_never_arrived_still_works() -> None:
    """Vendors don't guarantee webhook order or delivery. Found by the load
    test: a lost call-started event made every later tool call on that call a
    500, which the caller hears as silence. The first event we do get for an
    unknown call now registers it."""
    settings = Settings(_env_file=None, app_env="test", vapi_webhook_secret=SECRET)
    sync_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url)
    redis = sync_redis.Redis.from_url(settings.redis_url)
    tenant_id = uuid.uuid4()
    number = f"+1999{uuid.uuid4().int % 10**7:07d}"
    vendor_call_id = f"itest-late-{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical) "
                "VALUES (:id, :slug, 'Late', 'dental')"
            ),
            {"id": str(tenant_id), "slug": f"late-{tenant_id.hex[:8]}"},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO phone_numbers (tenant_id, e164) VALUES (:t, :e)"),
            {"t": str(tenant_id), "e": number},
        )
        conn.execute(
            text(
                "INSERT INTO agent_configs (tenant_id, version, is_active, system_prompt, "
                "first_message) VALUES (:t, 1, true, 'You are a receptionist.', 'Hello!')"
            ),
            {"t": str(tenant_id)},
        )
    try:
        with TestClient(create_app(settings)) as client:

            def post(fixture_name: str):
                body = json.dumps(_fixture(fixture_name, number, vendor_call_id))
                return client.post(
                    "/webhooks/voice/vapi", content=body, headers={"x-vapi-secret": SECRET}
                )

            # No call-started event: straight to a tool call, then the end.
            tool = post("vapi_tool_calls.json")
            assert tool.status_code == 200 and "results" in tool.json()
            assert post("vapi_end_of_call_report.json").json() == {"status": "accepted"}

        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            calls = conn.execute(
                text("SELECT ended_at FROM calls WHERE vendor_call_id = :v"), {"v": vendor_call_id}
            ).all()
        assert len(calls) == 1 and calls[0].ended_at is not None  # one call, completed
    finally:
        for item in redis.lrange(POST_CALL_QUEUE, 0, -1):
            if str(tenant_id) in item.decode():
                redis.lrem(POST_CALL_QUEUE, 0, item)
        redis.delete(f"call:{vendor_call_id}")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()
        redis.close()
