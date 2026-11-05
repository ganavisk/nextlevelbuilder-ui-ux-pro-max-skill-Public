"""Test Lab, through the real HTTP routes against live Postgres + Redis.

The model is scripted (no API key needed) but everything it triggers is real:
tool execution, bookings into bays, transcript and trace rows. The script also
checks the message shapes it receives, because a malformed tool_use /
tool_result history is accepted by a fake and rejected by the real API.
"""

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.queue import POST_CALL_QUEUE
from app.core.security import hash_password
from app.main import create_app
from app.modules.conversation.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
)
from tests.conftest import requires_services
from tests.fakes import FakeCalendarProvider

TZ = "America/Denver"
HOURS = {d: [["07:30", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
SETTINGS = {
    "scheduling": {
        "bays": 3,
        "services": [{"name": "Oil change", "duration_minutes": 45}],
        "booking_fields": [
            {"key": "vehicle", "description": "Year, make and model"},
            {"key": "mileage", "description": "Approximate mileage"},
        ],
    }
}


def _next_weekday_10am() -> str:
    day = datetime.now(ZoneInfo(TZ)) + timedelta(days=14)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.strftime("%Y-%m-%dT10:00")


class ScriptedLLM(LLMProvider):
    """Plays a caller-facing agent: checks availability, books, then answers."""

    def __init__(self, starts_at: str, *, loop_forever: bool = False) -> None:
        self.starts_at = starts_at
        self.loop_forever = loop_forever
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest, *, fast: bool = False) -> CompletionResult:
        self.requests.append(request)
        n = len(self.requests)
        if self.loop_forever:
            return self._tool("check_availability", {"service": "Oil change"}, f"loop-{n}")
        if n == 1:
            return self._tool(
                "check_availability",
                {"service": "Oil change", "preferred_time": self.starts_at},
                "tu_1",
            )
        if n == 2:
            return self._tool(
                "book_appointment",
                {
                    "service": "Oil change",
                    "starts_at": self.starts_at,
                    "customer_name": "Priya Raman",
                    "vehicle": "2019 Toyota RAV4",
                    "mileage": "41,000",
                },
                "tu_2",
            )
        return CompletionResult("You're booked in. See you then!", [], 10, 5, "scripted")

    @staticmethod
    def _tool(name: str, arguments: dict, tool_id: str) -> CompletionResult:
        call = {"id": tool_id, "name": name, "input": arguments}
        return CompletionResult("", [call], 10, 5, "scripted")


class DownLLM(LLMProvider):
    async def complete(self, request: CompletionRequest, *, fast: bool = False) -> CompletionResult:
        raise RuntimeError("no API key")


def _engine():
    url = Settings(_env_file=None, app_env="test").database_url
    return create_engine(url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1))


def _seed_shop(engine, slug: str, *, email_prefix: str = "") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, vertical, timezone, business_hours, "
                "settings) VALUES (:id, :slug, 'Lab Garage', 'auto_repair', :tz, "
                "CAST(:hours AS jsonb), CAST(:settings AS jsonb))"
            ),
            {
                "id": str(tenant_id),
                "slug": slug,
                "tz": TZ,
                "hours": json.dumps(HOURS),
                "settings": json.dumps(SETTINGS),
            },
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO agent_configs (tenant_id, version, is_active, system_prompt, "
                "first_message, qualification, escalation_policy) VALUES (:t, 1, true, "
                "'You are a service advisor.', 'Thanks for calling Lab Garage!', '[]', "
                '\'{"callback_promise": "We will call you back."}\')'
            ),
            {"t": str(tenant_id)},
        )
        for role in ("owner", "viewer"):
            conn.execute(
                text(
                    "INSERT INTO users (tenant_id, email, display_name, role, password_hash) "
                    "VALUES (:t, :e, :r, :r, :h)"
                ),
                {
                    "t": str(tenant_id),
                    "e": f"{email_prefix}{role}@lab.example",
                    "r": role,
                    "h": hash_password("pw"),
                },
            )
    return tenant_id


@pytest.fixture
def lab():
    engine = _engine()
    slug = f"lab-{uuid.uuid4().hex[:8]}"
    tenant_id = _seed_shop(engine, slug)
    starts_at = _next_weekday_10am()
    llm = ScriptedLLM(starts_at)
    calendar = FakeCalendarProvider()
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:
        app.state.llm_provider = llm
        app.state.calendar_provider = calendar

        def login(role: str = "owner", slug_: str = slug, prefix: str = "") -> dict:
            token = client.post(
                "/api/v1/auth/login",
                json={
                    "tenant_slug": slug_,
                    "email": f"{prefix}{role}@lab.example",
                    "password": "pw",
                },
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        yield {
            "client": client,
            "app": app,
            "login": login,
            "llm": llm,
            "calendar": calendar,
            "tenant_id": tenant_id,
            "engine": engine,
            "starts_at": starts_at,
        }
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
    engine.dispose()


def _rows(engine, tenant_id, sql: str, **params):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return conn.execute(text(sql), {"t": str(tenant_id), **params}).all()


def _start(lab) -> tuple[dict, str]:
    headers = lab["login"]()
    r = lab["client"].post("/api/v1/test-lab/sessions", headers=headers, json={})
    assert r.status_code == 200, r.json()
    return headers, r.json()["session_id"]


@requires_services
def test_status_describes_the_shop(lab) -> None:
    body = lab["client"].get("/api/v1/test-lab/status", headers=lab["login"]()).json()
    assert body["llm_available"] is True  # a non-Anthropic provider is injected
    assert body["bays"] == 3
    assert body["services"] == ["Oil change"]
    assert [f["key"] for f in body["booking_fields"]] == ["vehicle", "mileage"]


@requires_services
def test_a_simulated_call_books_a_real_appointment_and_traces_every_tool(lab) -> None:
    client, engine, tenant_id = lab["client"], lab["engine"], lab["tenant_id"]
    headers, session_id = _start(lab)
    redis = sync_redis.Redis.from_url(Settings(_env_file=None).redis_url)
    confirmations_before = redis.llen("queue:confirmations")

    r = client.post(
        f"/api/v1/test-lab/sessions/{session_id}/messages",
        headers=headers,
        json={"text": "Hi, can I get an oil change for my RAV4?"},
    )
    assert r.status_code == 200, r.json()
    turn = r.json()
    assert turn["reply"] == "You're booked in. See you then!"
    assert [e["tool"] for e in turn["tool_events"]] == ["check_availability", "book_appointment"]
    assert "all set" in turn["tool_events"][1]["result"].lower()
    assert all(e["latency_ms"] >= 0 for e in turn["tool_events"])

    # The booking is real: bay 1, with the vehicle details, linked to the call.
    appts = _rows(
        engine, tenant_id, "SELECT bay, details, call_id FROM appointments WHERE tenant_id = :t"
    )
    assert len(appts) == 1
    assert appts[0].bay == 1
    assert appts[0].details == {"vehicle": "2019 Toyota RAV4", "mileage": "41,000"}
    assert str(appts[0].call_id) == session_id
    # ...but nobody is texted: the caller number is made up.
    assert redis.llen("queue:confirmations") == confirmations_before
    redis.close()

    # Transcript and trace are persisted.
    turns = _rows(
        engine, tenant_id, "SELECT turns FROM transcripts WHERE call_id = :c", c=session_id
    )[0].turns
    assert [t["role"] for t in turns] == ["assistant", "caller", "assistant"]
    events = _rows(
        engine, tenant_id, "SELECT payload FROM call_events WHERE call_id = :c", c=session_id
    )
    assert {e.payload["tool"] for e in events} == {"check_availability", "book_appointment"}


@requires_services
def test_the_model_sees_the_real_prompt_tools_and_well_formed_tool_history(lab) -> None:
    headers, session_id = _start(lab)
    lab["client"].post(
        f"/api/v1/test-lab/sessions/{session_id}/messages",
        headers=headers,
        json={"text": "Oil change please"},
    )
    first, second, third = lab["llm"].requests

    # Anthropic tool format, specialised for this tenant.
    book = next(t for t in first.tools if t["name"] == "book_appointment")
    assert {"vehicle", "mileage"} <= set(book["input_schema"]["properties"])
    # The production prompt plus the date context the scheduling tools need.
    assert "Year, make and model" in first.system
    assert "Current local time at the business" in first.system
    assert "Thanks for calling Lab Garage!" in first.system
    # History: user text, then assistant tool_use, then user tool_result with the same id.
    assert first.messages == [{"role": "user", "content": "Oil change please"}]
    assistant, results = second.messages[1], second.messages[2]
    assert assistant["role"] == "assistant" and assistant["content"][0]["type"] == "tool_use"
    assert results["role"] == "user"
    assert results["content"][0] == {
        "type": "tool_result",
        "tool_use_id": assistant["content"][0]["id"],
        "content": results["content"][0]["content"],
    }
    assert len(third.messages) == 5


@requires_services
def test_the_board_shows_the_booking_in_its_bay(lab) -> None:
    headers, session_id = _start(lab)
    lab["client"].post(
        f"/api/v1/test-lab/sessions/{session_id}/messages",
        headers=headers,
        json={"text": "book me"},
    )
    day = lab["starts_at"][:10]
    board = lab["client"].get(f"/api/v1/test-lab/board?day={day}", headers=headers).json()
    assert board["bays"] == 3
    assert board["opening_hours"] == [["07:30", "18:00"]]
    assert len(board["appointments"]) == 1
    appt = board["appointments"][0]
    assert appt["bay"] == 1 and appt["simulated"] is True
    assert appt["details"]["vehicle"] == "2019 Toyota RAV4"


@requires_services
def test_activity_feed_lists_tool_calls_newest_first_and_polls_incrementally(lab) -> None:
    client = lab["client"]
    headers, session_id = _start(lab)
    client.post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "hi"}
    )
    items = client.get("/api/v1/test-lab/activity", headers=headers).json()
    assert [i["tool"] for i in items[:2]] == ["book_appointment", "check_availability"]
    assert items[0]["simulated"] is True and items[0]["latency_ms"] is not None

    newer = client.get(
        "/api/v1/test-lab/activity", headers=headers, params={"after": items[0]["occurred_at"]}
    ).json()
    assert newer == []


@requires_services
def test_the_tool_bench_works_without_a_model(lab) -> None:
    lab["app"].state.llm_provider = DownLLM()
    headers, session_id = _start(lab)
    r = lab["client"].post(
        f"/api/v1/test-lab/sessions/{session_id}/tools/book_appointment",
        headers=headers,
        json={
            "arguments": {
                "service": "Oil change",
                "starts_at": lab["starts_at"],
                "customer_name": "Sam",
                "vehicle": "2012 Honda Fit",
            }
        },
    )
    assert r.status_code == 200, r.json()
    assert "all set" in r.json()["result"].lower()

    unknown = lab["client"].post(
        f"/api/v1/test-lab/sessions/{session_id}/tools/drop_database", headers=headers, json={}
    )
    assert unknown.status_code == 422


@requires_services
def test_a_missing_model_is_a_clear_503_not_a_crash(lab) -> None:
    lab["app"].state.llm_provider = DownLLM()
    headers, session_id = _start(lab)
    r = lab["client"].post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "hello"}
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "llm_unavailable"


@requires_services
def test_a_model_stuck_calling_tools_is_cut_off(lab) -> None:
    lab["app"].state.llm_provider = ScriptedLLM(lab["starts_at"], loop_forever=True)
    headers, session_id = _start(lab)
    turn = (
        lab["client"]
        .post(
            f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "hi"}
        )
        .json()
    )
    assert len(turn["tool_events"]) == 5
    assert "call you back" in turn["reply"]


@requires_services
def test_simulated_calls_stay_out_of_analytics(lab) -> None:
    headers, session_id = _start(lab)
    lab["client"].post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "hi"}
    )
    summary = lab["client"].get("/api/v1/analytics/summary", headers=headers).json()
    assert summary["total_calls"] == 0


@requires_services
def test_ending_a_call_queues_the_summary_and_closes_the_session(lab) -> None:
    client = lab["client"]
    headers, session_id = _start(lab)
    redis = sync_redis.Redis.from_url(Settings(_env_file=None).redis_url)
    assert (
        client.post(f"/api/v1/test-lab/sessions/{session_id}/end", headers=headers).status_code
        == 200
    )
    jobs = [json.loads(j) for j in redis.lrange(POST_CALL_QUEUE, 0, -1)]
    assert any(j["call_id"] == session_id for j in jobs)
    for job in redis.lrange(POST_CALL_QUEUE, 0, -1):
        if session_id in job.decode():
            redis.lrem(POST_CALL_QUEUE, 0, job)
    redis.close()

    after = client.post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "hi"}
    )
    assert after.status_code == 422


@requires_services
def test_discarding_a_session_removes_what_it_created_including_calendar_events(lab) -> None:
    client, engine, tenant_id = lab["client"], lab["engine"], lab["tenant_id"]
    headers, session_id = _start(lab)
    client.post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "book"}
    )
    booked_event = lab["calendar"].booked and "evt-1"

    r = client.delete(f"/api/v1/test-lab/sessions/{session_id}", headers=headers)
    assert r.status_code == 200 and r.json() == {"removed_appointments": 1}
    assert lab["calendar"].cancelled == [booked_event]
    assert _rows(engine, tenant_id, "SELECT id FROM appointments WHERE tenant_id = :t") == []
    assert _rows(engine, tenant_id, "SELECT id FROM calls WHERE tenant_id = :t") == []


@requires_services
def test_viewers_cannot_use_the_lab(lab) -> None:
    r = lab["client"].post("/api/v1/test-lab/sessions", headers=lab["login"]("viewer"), json={})
    assert r.status_code == 403


@requires_services
def test_another_tenants_admin_cannot_reach_a_session(lab) -> None:
    headers, session_id = _start(lab)
    engine = lab["engine"]
    other_slug = f"lab-other-{uuid.uuid4().hex[:6]}"
    other_id = _seed_shop(engine, other_slug, email_prefix="other-")
    try:
        other = lab["login"]("owner", other_slug, "other-")
        r = lab["client"].post(
            f"/api/v1/test-lab/sessions/{session_id}/messages", headers=other, json={"text": "hi"}
        )
        assert r.status_code == 404
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(other_id)})


@requires_services
def test_each_bench_booking_is_a_new_booking_so_capacity_can_be_tested(lab) -> None:
    """On a real call, the same slot booked twice is a retry and replays. On the
    bench every run is deliberate: four runs at one 10:00 slot fill the three
    bays and the fourth is told it's taken."""
    headers, session_id = _start(lab)
    results = []
    for name in ("Ana", "Ben", "Cy", "Dee"):
        r = lab["client"].post(
            f"/api/v1/test-lab/sessions/{session_id}/tools/book_appointment",
            headers=headers,
            json={
                "arguments": {
                    "service": "Oil change",
                    "starts_at": lab["starts_at"],
                    "customer_name": name,
                }
            },
        )
        results.append(r.json()["result"].lower())
    assert sum("all set" in r for r in results) == 3
    assert "taken" in results[3]
    bays = _rows(
        lab["engine"],
        lab["tenant_id"],
        "SELECT bay FROM appointments WHERE tenant_id = :t AND status = 'confirmed' ORDER BY bay",
    )
    assert [b.bay for b in bays] == [1, 2, 3]


@requires_services
def test_a_repeated_booking_within_a_chat_still_replays(lab) -> None:
    """The model calling book_appointment twice for the same slot in one call
    must not take two bays."""
    headers, session_id = _start(lab)
    client = lab["client"]
    client.post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "book"}
    )
    lab["llm"].requests.clear()  # the script books again on its next turn
    client.post(
        f"/api/v1/test-lab/sessions/{session_id}/messages", headers=headers, json={"text": "again"}
    )
    bays = _rows(
        lab["engine"], lab["tenant_id"], "SELECT bay FROM appointments WHERE tenant_id = :t"
    )
    assert [b.bay for b in bays] == [1]


@requires_services
def test_the_appointments_list_marks_test_bookings(lab) -> None:
    """Test Lab bookings are real rows, so staff must be able to tell them
    apart from customers' on the Appointments page (and after cancelling)."""
    headers, session_id = _start(lab)
    client = lab["client"]
    client.post(
        f"/api/v1/test-lab/sessions/{session_id}/tools/book_appointment",
        headers=headers,
        json={
            "arguments": {
                "service": "Oil change",
                "starts_at": lab["starts_at"],
                "customer_name": "Ana",
            }
        },
    )
    items = client.get("/api/v1/appointments", headers=headers).json()["items"]
    assert len(items) == 1 and items[0]["simulated"] is True
    cancelled = client.post(f"/api/v1/appointments/{items[0]['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["simulated"] is True
