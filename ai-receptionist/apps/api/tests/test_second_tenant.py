"""Phase 8 exit criterion: a second business onboards with zero code, and
neither business can see the other's data.

Tenant #1 is the dental practice and tenant #2 the auto repair shop. Both are
onboarded by the real onboarding path (`scripts/seed_demo.py: seed`) from
their YAML in config/tenants, changed only to get unique slugs, numbers and
logins. Then each takes calls through the real signed Vapi webhook and is
checked from the outside in:
- the dashboard API, as each tenant's owner;
- Postgres row-level security, on every tenant table the catalog lists (not
  a hand-kept list, so a new table can't be missed);
- knowledge retrieval, where both businesses hold similar text.

The "zero code" half is also checked statically: nothing under src/app names
a business, its vertical or its services.
"""

import functools
import importlib.util
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import redis as sync_redis
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.queue import CONFIRMATIONS_QUEUE, CRM_QUEUE, POST_CALL_QUEUE
from app.main import create_app
from app.modules.conversation.knowledge import ingest_document
from app.modules.conversation.retrieval import retrieve_relevant_chunks
from tests.conftest import requires_services
from tests.fakes import FakeCalendarProvider, FakeEmbeddingProvider

API = Path(__file__).resolve().parents[1]
CONFIG = API.parents[1] / "config" / "tenants"
SECRET = "second-tenant-secret"

_spec = importlib.util.spec_from_file_location("seed_demo", API / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)  # type: ignore[union-attr]


# ── zero code, statically ───────────────────────────────────────────────


def test_no_code_names_a_business_its_vertical_or_its_services() -> None:
    """Everything that makes a dental practice different from a law firm or
    an auto shop has to live in config/tenants, never in src/app. A
    tenant-specific branch is how 'onboard by config' quietly stops being true."""
    specifics: set[str] = set()
    for path in CONFIG.glob("*.yaml"):
        doc = yaml.safe_load(path.read_text())
        specifics |= {doc["tenant"]["slug"], doc["tenant"]["name"], doc["tenant"]["vertical"]}
        specifics |= {s["name"] for s in doc.get("scheduling", {}).get("services", [])}
    source = "\n".join(p.read_text() for p in (API / "src" / "app").rglob("*.py"))
    found = sorted(s for s in specifics if re.search(rf"[\"']{re.escape(s)}[\"']", source))
    assert found == [], f"tenant-specific values in code: {found}"


# ── zero code, end to end ───────────────────────────────────────────────


def _unique_copy(name: str, tmp: Path, tag: str) -> tuple[Path, dict]:
    """The example YAML with only identity changed (slug, login), so the test
    never collides with a seeded demo tenant."""
    doc = yaml.safe_load((CONFIG / f"{name}.yaml").read_text())
    doc["tenant"]["slug"] = f"{doc['tenant']['slug']}-{tag}"
    doc["dashboard"]["owner_email"] = f"owner-{tag}@{name}.example"
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path, doc


def _next_weekday(tz: str, hour: int) -> str:
    day = datetime.now(ZoneInfo(tz)) + timedelta(days=7)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return f"{day:%Y-%m-%d}T{hour:02d}:00"


def _engine():
    url = Settings(_env_file=None, app_env="test").database_url
    return create_engine(url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1))


@pytest.fixture
def two_businesses(tmp_path):
    tag = uuid.uuid4().hex[:8]
    settings = Settings(app_env="test", vapi_webhook_secret=SECRET)
    app = create_app(settings)
    engine = _engine()
    seeded: list[uuid.UUID] = []
    with TestClient(app) as client:
        app.state.calendar_provider = FakeCalendarProvider()
        tenants = {}
        for n, name in enumerate(("example-dental", "example-auto"), start=1):
            path, doc = _unique_copy(name, tmp_path, tag)
            number = f"+1999{uuid.uuid4().int % 10**7:07d}"
            tenant_id = client.portal.call(seed_demo.seed, path, number)
            assert tenant_id is not None
            seeded.append(tenant_id)
            tenants[n] = {"id": tenant_id, "number": number, "doc": doc}
        yield client, tenants, engine
    with engine.begin() as conn:
        for tenant_id in seeded:
            conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
    r = sync_redis.Redis.from_url(settings.redis_url)
    for queue in (POST_CALL_QUEUE, CONFIRMATIONS_QUEUE, CRM_QUEUE):
        for raw in r.lrange(queue, 0, -1):
            if any(str(t) in raw.decode() for t in seeded):
                r.lrem(queue, 0, raw)
    r.close()
    engine.dispose()


def _call(client, number: str, steps: list[tuple[str, dict]]) -> list[str]:
    """One phone call through the signed webhook; returns the tool results."""
    vendor_id = f"t2-{uuid.uuid4().hex[:12]}"
    base = {"call": {"id": vendor_id, "customer": {"number": "+17205550142"}}}
    base["phoneNumber"] = {"number": number}

    def post(message: dict):
        r = client.post(
            "/webhooks/voice/vapi",
            content=json.dumps({"message": {**message, **base}}),
            headers={"x-vapi-secret": SECRET},
        )
        assert r.status_code == 200, r.text
        return r.json()

    post({"type": "status-update", "status": "in-progress"})
    results = []
    for name, arguments in steps:
        reply = post(
            {
                "type": "tool-calls",
                "toolCallList": [{"id": "tc", "name": name, "arguments": arguments}],
            }
        )
        results.append(reply["results"][0]["result"])
    post({"type": "end-of-call-report", "endedReason": "customer-ended-call", "artifact": {}})
    return results


def _login(client, doc: dict) -> dict:
    token = client.post(
        "/api/v1/auth/login",
        json={
            "tenant_slug": doc["tenant"]["slug"],
            "email": doc["dashboard"]["owner_email"],
            "password": doc["dashboard"]["owner_password"],
        },
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@requires_services
def test_a_second_business_onboards_by_config_and_stays_isolated(two_businesses) -> None:
    client, t, engine = two_businesses
    dental, auto = t[1], t[2]

    # ── both work, each by its own configuration ──
    dental_service = dental["doc"]["scheduling"]["services"][0]["name"]
    dental_said = _call(
        client,
        dental["number"],
        [
            (
                "book_appointment",
                {
                    "service": dental_service,
                    "starts_at": _next_weekday(dental["doc"]["tenant"]["timezone"], 10),
                    "customer_name": "Dana Lee",
                },
            ),
            ("request_callback", {"reason": "Billing question", "customer_name": "Dana Lee"}),
        ],
    )
    assert dental_said[0].startswith("You're all set"), dental_said[0]

    auto_service = auto["doc"]["scheduling"]["services"][0]["name"]
    auto_start = _next_weekday(auto["doc"]["tenant"]["timezone"], 10)

    def book_auto(name: str, vehicle: str) -> str:
        # Separate calls: two bookings for one slot within the same call are
        # a retry by design, and replay the first.
        return _call(
            client,
            auto["number"],
            [
                (
                    "book_appointment",
                    {
                        "service": auto_service,
                        "starts_at": auto_start,
                        "customer_name": name,
                        "vehicle": vehicle,
                    },
                )
            ],
        )[0]

    auto_said = [
        book_auto("Marcus Bell", "2014 Ford F-150"),
        book_auto("Ines Ortega", "2021 Honda Civic"),
    ]
    auto_said += _call(
        client,
        auto["number"],
        [("request_callback", {"reason": "Invoice question", "customer_name": "Marcus Bell"})],
    )
    # The auto shop's own configuration at work: its booking field is read
    # back, and its 3 bays take two bookings at the same time.
    assert "for your 2014 Ford F-150" in auto_said[0], auto_said[0]
    assert auto_said[1].startswith("You're all set"), auto_said[1]

    # ── the dashboard: each owner sees only their own business ──
    as_dental, as_auto = _login(client, dental["doc"]), _login(client, auto["doc"])
    for path, own_dental, own_auto in (
        ("/api/v1/calls", 1, 3),
        ("/api/v1/appointments", 1, 2),
        ("/api/v1/callback-requests", 1, 1),
    ):
        seen_dental = client.get(path, headers=as_dental).json()
        seen_auto = client.get(path, headers=as_auto).json()
        assert seen_dental["total"] == own_dental, path
        assert seen_auto["total"] == own_auto, path

    auto_call_id = client.get("/api/v1/calls", headers=as_auto).json()["items"][0]["id"]
    auto_appt_id = client.get("/api/v1/appointments", headers=as_auto).json()["items"][0]["id"]
    assert client.get(f"/api/v1/calls/{auto_call_id}", headers=as_dental).status_code == 404
    cancel = client.post(f"/api/v1/appointments/{auto_appt_id}/cancel", headers=as_dental)
    assert cancel.status_code == 404
    still = client.get("/api/v1/appointments", headers=as_auto).json()["items"]
    assert all(a["status"] == "confirmed" for a in still)  # the attempt changed nothing

    # ── the database: every tenant table, from the catalog ──
    with engine.begin() as conn:
        tables = [
            row.relname
            for row in conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN information_schema.columns col ON col.table_name = c.relname "
                    "AND col.column_name = 'tenant_id' "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                )
            )
        ]
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(dental["id"])})
        for table in tables:
            leaked = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id <> :t"),
                {"t": str(dental["id"])},
            ).scalar_one()
            assert leaked == 0, f"{table}: tenant #1 can see {leaked} rows of another tenant"
    assert len(tables) >= 15  # every tenant table, including ones added later


@requires_services
def test_knowledge_retrieval_never_crosses_businesses(two_businesses) -> None:
    """Asked the other business's exact words (distance 0 to its chunk, the
    strongest pull there is), each business still gets nothing of the
    other's. Its own words find its own chunk."""
    client, t, engine = two_businesses
    embeddings = FakeEmbeddingProvider()
    chunks: dict[int, list[str]] = {}
    for n in (1, 2):
        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t[n]["id"])}
            )
            doc_ids = [r[0] for r in conn.execute(text("SELECT id FROM knowledge_docs"))]
        for doc_id in doc_ids:
            client.portal.call(ingest_document, t[n]["id"], doc_id, embeddings)
        with engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(t[n]["id"])}
            )
            chunks[n] = [r[0] for r in conn.execute(text("SELECT content FROM knowledge_chunks"))]
        assert chunks[n], f"tenant #{n} has no chunks"

    def ask(n: int, question: str) -> list[str]:
        found = client.portal.call(
            functools.partial(retrieve_relevant_chunks, t[n]["id"], question, embeddings)
        )
        return [c.content for c in found]

    for own, other in ((1, 2), (2, 1)):
        for text_of_other in chunks[other]:
            leaked = set(ask(own, text_of_other)) & set(chunks[other])
            assert not leaked, f"tenant #{own} retrieved tenant #{other}'s chunk"
        assert ask(own, chunks[own][0])[0] == chunks[own][0]
