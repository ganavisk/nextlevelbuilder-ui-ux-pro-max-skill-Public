"""Dashboard saves reach the live phone agent, and the dashboard says so.

Before this, a Settings save changed the database only: callers kept hearing
the old prompt until someone ran `make provision`. These tests drive the real
routes and the real job handler against Postgres and Redis, with a fake voice
vendor in place of Vapi.
"""

import functools
import json
import uuid
from typing import Any

import httpx
import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.errors import PermanentIntegrationError
from app.core.queue import VOICE_SYNC_QUEUE
from app.core.security import hash_password
from app.main import create_app
from app.modules.telephony.providers.base import (
    AgentDefinition,
    NormalizedCallEvent,
    ToolCallRequest,
    VoiceProvider,
)
from app.workers.voice_sync import Deps, handle_voice_sync
from tests.conftest import requires_services


class FakeVoiceProvider(VoiceProvider):
    """Records what would be pushed to the vendor."""

    def __init__(self, *, agent_id: str = "asst-1", fail: Exception | None = None) -> None:
        self.agent_id = agent_id
        self.fail = fail
        self.synced: list[tuple[AgentDefinition, str]] = []
        self.attached: list[tuple[str, str]] = []
        self.during_push: Any = None  # called inside sync_agent, to simulate a race

    async def sync_agent(self, definition: AgentDefinition, *, existing_agent_id: str = "") -> str:
        if self.during_push:
            self.during_push()
        if self.fail:
            raise self.fail
        self.synced.append((definition, existing_agent_id))
        return self.agent_id

    async def attach_number(self, vendor_agent_id: str, e164_number: str) -> str:
        self.attached.append((vendor_agent_id, e164_number))
        return f"num-{e164_number}"

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> None:
        raise NotImplementedError

    def parse_event(self, payload: dict[str, Any]) -> NormalizedCallEvent:
        raise NotImplementedError

    def extract_tool_calls(self, event: NormalizedCallEvent) -> list[ToolCallRequest]:
        raise NotImplementedError

    def format_tool_results(self, results: list[tuple[str, str]]) -> dict[str, Any]:
        raise NotImplementedError


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://api.vapi.ai/assistant/asst-1")
    return httpx.HTTPStatusError(
        "vendor error", request=request, response=httpx.Response(status, request=request)
    )


def _engine():
    url = Settings(_env_file=None, app_env="test").database_url
    return create_engine(url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1))


def _seed(engine, *, connected: bool) -> tuple[uuid.UUID, str]:
    tenant_id = uuid.uuid4()
    slug = f"vs-{tenant_id.hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, settings) "
                "VALUES (:id, :slug, 'Sync Garage', 'auto_repair', 'America/Denver', "
                "CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": slug,
                "settings": json.dumps({"scheduling": {"services": [{"name": "Oil change"}]}}),
            },
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO agent_configs (tenant_id, version, is_active, system_prompt, "
                "first_message, voice_id, qualification, escalation_policy) VALUES (:t, 1, "
                "true, 'You are a service advisor.', 'Thanks for calling!', 'voice-1', '[]', "
                "'{}')"
            ),
            {"t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO phone_numbers (tenant_id, e164, vendor_agent_id) VALUES (:t, :e, :a)"
            ),
            {
                "t": str(tenant_id),
                "e": f"+1555{tenant_id.int % 10_000_000:07d}",
                "a": "asst-1" if connected else "",
            },
        )
        for role in ("owner", "viewer"):
            conn.execute(
                text(
                    "INSERT INTO users (tenant_id, email, display_name, role, password_hash) "
                    "VALUES (:t, :e, :r, :r, :h)"
                ),
                {
                    "t": str(tenant_id),
                    "e": f"{role}@{slug}.example",
                    "r": role,
                    "h": hash_password("pw"),
                },
            )
    return tenant_id, slug


def _make_fixture(name: str, *, connected: bool):
    @pytest.fixture(name=name)
    def fixture():
        engine = _engine()
        tenant_id, slug = _seed(engine, connected=connected)
        settings = Settings(app_env="test")
        queue = sync_redis.Redis.from_url(settings.redis_url)
        queue.delete(VOICE_SYNC_QUEUE)
        app = create_app(settings)
        with TestClient(app) as client:

            def login(role: str = "owner") -> dict:
                token = client.post(
                    "/api/v1/auth/login",
                    json={"tenant_slug": slug, "email": f"{role}@{slug}.example", "password": "pw"},
                ).json()["access_token"]
                return {"Authorization": f"Bearer {token}"}

            def jobs() -> list[dict]:
                return [json.loads(raw) for raw in queue.lrange(VOICE_SYNC_QUEUE, 0, -1)]

            def run_job(provider: FakeVoiceProvider, *, attempts: int = 0) -> None:
                """Run the real handler on the app's event loop."""
                job = {"type": "voice_sync", "tenant_id": str(tenant_id), "attempts": attempts}
                deps = Deps(settings=settings, voice=provider)
                client.portal.call(functools.partial(handle_voice_sync, job, deps=deps))

            def status(headers: dict) -> dict:
                return client.get("/api/v1/tenants/me/voice-sync", headers=headers).json()

            def row(sql: str):
                with engine.begin() as conn:
                    conn.execute(
                        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
                    )
                    return conn.execute(text(sql), {"t": str(tenant_id)}).one()

            yield {
                "client": client,
                "login": login,
                "jobs": jobs,
                "run_job": run_job,
                "status": status,
                "row": row,
                "engine": engine,
                "tenant_id": tenant_id,
            }
        queue.delete(VOICE_SYNC_QUEUE)
        queue.close()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
        engine.dispose()

    return fixture


shop_fixture = _make_fixture("shop", connected=True)
unprovisioned_fixture = _make_fixture("unprovisioned", connected=False)


# ── what triggers a sync ────────────────────────────────────────────────


@requires_services
def test_a_persona_change_goes_live_on_the_phone_agent(shop) -> None:
    headers = shop["login"]()
    assert shop["status"](headers)["state"] == "live"

    r = shop["client"].patch(
        "/api/v1/tenants/me/agent-config",
        headers=headers,
        json={"system_prompt": "You are a friendly brake specialist."},
    )
    assert r.status_code == 200
    assert len(shop["jobs"]()) == 1
    assert shop["status"](headers)["state"] == "updating"

    vendor = FakeVoiceProvider()
    shop["run_job"](vendor)

    definition, existing_id = vendor.synced[0]
    assert existing_id == "asst-1"  # updated in place, not duplicated
    assert definition.system_prompt.startswith("You are a friendly brake specialist.")
    after = shop["status"](headers)
    assert after["state"] == "live" and after["synced_at"] is not None


@requires_services
def test_a_timezone_change_reaches_the_agents_clock(shop) -> None:
    headers = shop["login"]()
    r = shop["client"].patch(
        "/api/v1/tenants/me", headers=headers, json={"timezone": "America/Chicago"}
    )
    assert r.status_code == 200
    vendor = FakeVoiceProvider()
    shop["run_job"](vendor)
    assert vendor.synced[0][0].timezone == "America/Chicago"


@requires_services
def test_a_new_booking_field_changes_the_agents_tools_and_is_synced(shop) -> None:
    headers = shop["login"]()
    shop["client"].patch(
        "/api/v1/tenants/me",
        headers=headers,
        json={
            "settings": {
                "scheduling": {
                    "booking_fields": [{"key": "vehicle", "description": "Year, make, model"}]
                }
            }
        },
    )
    assert len(shop["jobs"]()) == 1


@requires_services
def test_changes_that_dont_affect_the_agent_queue_nothing(shop) -> None:
    """Business hours are read live by the tools; CRM settings never reach the
    agent. Neither needs a push."""
    headers = shop["login"]()
    shop["client"].patch(
        "/api/v1/tenants/me",
        headers=headers,
        json={"business_hours": {"mon": [["08:00", "17:00"]]}},
    )
    shop["client"].patch(
        "/api/v1/tenants/me", headers=headers, json={"settings": {"crm": {"deal_min_score": 40}}}
    )
    assert shop["jobs"]() == []
    assert shop["row"]("SELECT voice_sync_requested FROM tenants WHERE id = :t")[0] == 0
    assert shop["status"](headers)["state"] == "live"


@requires_services
def test_an_unknown_timezone_is_refused_with_a_readable_message(shop) -> None:
    headers = shop["login"]()
    r = shop["client"].patch(
        "/api/v1/tenants/me", headers=headers, json={"timezone": "Mountain Time"}
    )
    assert r.status_code == 422
    assert "IANA" in r.json()["error"]["message"]
    assert shop["row"]("SELECT timezone FROM tenants WHERE id = :t")[0] == "America/Denver"
    assert shop["jobs"]() == []


@requires_services
def test_an_unprovisioned_tenant_saves_without_queueing_a_push(unprovisioned) -> None:
    """No number on the vendor yet: nothing to update. `make provision`
    pushes the current settings when it connects the number."""
    headers = unprovisioned["login"]()
    unprovisioned["client"].patch(
        "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": "Hi there!"}
    )
    assert unprovisioned["jobs"]() == []
    assert unprovisioned["status"](headers)["state"] == "not_connected"


# ── failures ────────────────────────────────────────────────────────────


@requires_services
def test_a_vendor_rejection_keeps_the_save_and_shows_a_retryable_failure(shop) -> None:
    headers = shop["login"]()
    r = shop["client"].patch(
        "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": "Hello!"}
    )
    assert r.status_code == 200  # the save stands whatever the vendor does

    with pytest.raises(PermanentIntegrationError):  # dead-lettered, not retried
        shop["run_job"](FakeVoiceProvider(fail=_http_error(400)))
    failed = shop["status"](headers)
    assert failed["state"] == "failed"
    assert "HTTP 400" in failed["error"]

    retry = shop["client"].post("/api/v1/tenants/me/voice-sync", headers=headers)
    assert retry.status_code == 200 and retry.json()["state"] == "updating"
    shop["run_job"](FakeVoiceProvider())
    live = shop["status"](headers)
    assert live["state"] == "live" and live["error"] == ""


@requires_services
def test_a_transient_failure_is_retried_and_only_reported_on_the_last_attempt(shop) -> None:
    headers = shop["login"]()
    shop["client"].patch(
        "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": "Hello!"}
    )
    with pytest.raises(httpx.HTTPStatusError):
        shop["run_job"](FakeVoiceProvider(fail=_http_error(503)), attempts=0)
    assert shop["status"](headers)["state"] == "updating"  # the queue will retry

    with pytest.raises(httpx.HTTPStatusError):
        shop["run_job"](FakeVoiceProvider(fail=_http_error(503)), attempts=4)  # 5th of 5
    assert shop["status"](headers)["state"] == "failed"


@requires_services
def test_a_failed_enqueue_is_shown_as_failed_not_left_updating(shop, monkeypatch) -> None:
    from app.modules.tenants import voice_sync

    async def broken(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(voice_sync, "enqueue", broken)
    headers = shop["login"]()
    r = shop["client"].patch(
        "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": "Hello!"}
    )
    assert r.status_code == 200
    status = shop["status"](headers)
    assert status["state"] == "failed" and "queue" in status["error"]


@requires_services
def test_viewers_cannot_trigger_a_push(shop) -> None:
    r = shop["client"].post("/api/v1/tenants/me/voice-sync", headers=shop["login"]("viewer"))
    assert r.status_code == 403


# ── ordering and vendor drift ───────────────────────────────────────────


@requires_services
def test_two_quick_saves_push_once_with_the_latest_settings(shop) -> None:
    headers = shop["login"]()
    for greeting in ("Hello!", "Hello again!"):
        shop["client"].patch(
            "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": greeting}
        )
    assert len(shop["jobs"]()) == 2

    vendor = FakeVoiceProvider()
    shop["run_job"](vendor)
    shop["run_job"](vendor)  # already live: skips the vendor
    assert [d.first_message for d, _ in vendor.synced] == ["Hello again!"]
    assert shop["status"](headers)["state"] == "live"


@requires_services
def test_a_save_that_lands_mid_push_is_not_reported_live(shop) -> None:
    """The job records the version it read *before* building the agent, so a
    save committed while the push is in flight stays "updating" until its own
    job runs."""
    headers = shop["login"]()
    shop["client"].patch(
        "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": "Hello!"}
    )
    vendor = FakeVoiceProvider()

    def concurrent_save() -> None:
        with shop["engine"].begin() as conn:
            conn.execute(
                text(
                    "UPDATE tenants SET voice_sync_requested = voice_sync_requested + 1 "
                    "WHERE id = :t"
                ),
                {"t": str(shop["tenant_id"])},
            )

    vendor.during_push = concurrent_save
    shop["run_job"](vendor)
    assert shop["status"](headers)["state"] == "updating"


@requires_services
def test_a_replaced_vendor_agent_gets_the_numbers_moved_to_it(shop) -> None:
    """If the stored assistant was deleted on the vendor side, sync_agent
    creates a new one; the numbers must follow, or calls ring a dead agent."""
    headers = shop["login"]()
    shop["client"].patch(
        "/api/v1/tenants/me/agent-config", headers=headers, json={"first_message": "Hello!"}
    )
    vendor = FakeVoiceProvider(agent_id="asst-2")
    shop["run_job"](vendor)

    assert [agent for agent, _ in vendor.attached] == ["asst-2"]
    stored = shop["row"]("SELECT vendor_agent_id FROM phone_numbers WHERE tenant_id = :t")[0]
    assert stored == "asst-2"
    assert shop["status"](headers)["state"] == "live"
