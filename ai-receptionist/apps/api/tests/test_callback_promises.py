"""Editing what callers are promised after asking for a call back.

Both texts are read aloud as typed, so the editor refuses what would come out
garbled, keeps the policy keys it doesn't show, and the preview names the
real next opening.
"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.errors import ValidationFailedError
from app.main import create_app
from app.modules.escalation.service import example_next_open, validate_promises
from app.modules.scheduling.availability import next_reopening
from tests.conftest import requires_services
from tests.test_after_hours import SHOP_HOURS
from tests.test_voice_sync import _engine, _seed

DENVER = ZoneInfo("America/Denver")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=DENVER)  # Nov 2 2026 is a Monday


# ── validation ──────────────────────────────────────────────────────────


def test_valid_promises_are_tidied() -> None:
    assert validate_promises("  We'll call\nyou back. ", " Closed; we call {next_open}. ") == {
        "callback_promise": "We'll call you back.",
        "after_hours_promise": "Closed; we call {next_open}.",
    }


@pytest.mark.parametrize(
    ("promise", "after", "message"),
    [
        ("", "", "can't be empty"),
        ("We'll call you {next_open}.", "", "only works in the after-hours promise"),
        ("We'll call.", "We open {next_opne}.", "{next_opne} isn't something"),
        ("We'll call.", "Hi {name}, we open {next_open}.", "{name} isn't something"),
        ("Bye {name}.", "", "{name} isn't something"),
        ("x" * 301, "", "too long"),
    ],
)
def test_promises_callers_would_hear_garbled_are_refused(promise, after, message) -> None:
    with pytest.raises(ValidationFailedError, match=re.escape(message)):
        validate_promises(promise, after)


# ── the preview's next opening ──────────────────────────────────────────


def test_while_open_the_preview_names_the_opening_after_today_closes() -> None:
    assert next_reopening(SHOP_HOURS, "America/Denver", _at(3, 10)) == _at(4, 7, 30)
    assert example_next_open(SHOP_HOURS, "America/Denver", _at(3, 10)) == "tomorrow at 7:30 am"


def test_on_saturday_the_preview_names_monday() -> None:
    assert example_next_open(SHOP_HOURS, "America/Denver", _at(7, 9)) == "Monday at 7:30 am"


def test_while_closed_the_preview_names_the_next_opening() -> None:
    assert example_next_open(SHOP_HOURS, "America/Denver", _at(3, 22)) == "tomorrow at 7:30 am"


def test_no_hours_no_preview() -> None:
    assert example_next_open({}, "America/Denver", _at(3, 10)) is None


# ── the API ─────────────────────────────────────────────────────────────


@pytest.fixture
def shop():
    engine = _engine()
    tenant_id, slug = _seed(engine, connected=False)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET business_hours = CAST(:h AS jsonb) WHERE id = :t"),
            {"h": json.dumps(SHOP_HOURS), "t": str(tenant_id)},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "UPDATE agent_configs SET escalation_policy = CAST(:p AS jsonb) "
                "WHERE tenant_id = :t"
            ),
            {
                "p": json.dumps(
                    {
                        "mode": "voicemail_callback",
                        "triggers": ["caller asks for a person"],
                        "callback_promise": "We'll call you back soon.",
                    }
                ),
                "t": str(tenant_id),
            },
        )
    app = create_app(Settings(app_env="test"))
    with TestClient(app) as client:

        def login(role: str = "owner") -> dict:
            token = client.post(
                "/api/v1/auth/login",
                json={"tenant_slug": slug, "email": f"{role}@{slug}.example", "password": "pw"},
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        yield client, login
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tenant_id)})
    engine.dispose()


@requires_services
def test_saving_keeps_the_policy_keys_the_form_does_not_show(shop) -> None:
    client, login = shop
    headers = login()
    r = client.put(
        "/api/v1/tenants/me/callback-promises",
        headers=headers,
        json={
            "callback_promise": "A service advisor will call you back within the hour.",
            "after_hours_promise": "We're closed, so we'll call you {next_open}.",
        },
    )
    assert r.status_code == 200, r.json()
    policy = r.json()["escalation_policy"]
    assert policy == {
        "mode": "voicemail_callback",
        "triggers": ["caller asks for a person"],
        "callback_promise": "A service advisor will call you back within the hour.",
        "after_hours_promise": "We're closed, so we'll call you {next_open}.",
    }
    # A new version, like every agent edit: the old one stays on file.
    active = client.get("/api/v1/tenants/me/agent-config", headers=headers).json()
    assert active["version"] == 2 and active["escalation_policy"] == policy


@requires_services
def test_clearing_the_after_hours_promise_removes_it(shop) -> None:
    client, login = shop
    headers = login()
    client.put(
        "/api/v1/tenants/me/callback-promises",
        headers=headers,
        json={"callback_promise": "We'll call.", "after_hours_promise": "Closed. {next_open}."},
    )
    r = client.put(
        "/api/v1/tenants/me/callback-promises",
        headers=headers,
        json={"callback_promise": "We'll call.", "after_hours_promise": ""},
    )
    assert "after_hours_promise" not in r.json()["escalation_policy"]


@requires_services
def test_a_mistyped_placeholder_is_refused_and_nothing_saved(shop) -> None:
    client, login = shop
    headers = login()
    r = client.put(
        "/api/v1/tenants/me/callback-promises",
        headers=headers,
        json={"callback_promise": "We'll call.", "after_hours_promise": "We open {next_opne}."},
    )
    assert r.status_code == 422
    assert "{next_opne}" in r.json()["error"]["message"]
    assert client.get("/api/v1/tenants/me/agent-config", headers=headers).json()["version"] == 1


@requires_services
def test_the_raw_agent_config_api_applies_the_same_checks(shop) -> None:
    client, login = shop
    r = client.patch(
        "/api/v1/tenants/me/agent-config",
        headers=login(),
        json={"escalation_policy": {"callback_promise": "Bye {name}."}},
    )
    assert r.status_code == 422


@requires_services
def test_viewers_can_preview_but_not_save(shop) -> None:
    client, login = shop
    viewer = login("viewer")
    preview = client.get("/api/v1/tenants/me/callback-promises/preview", headers=viewer)
    assert preview.status_code == 200
    assert preview.json()["next_open_example"].endswith("at 7:30 am") or preview.json()[
        "next_open_example"
    ].endswith("at 8 am")  # depends on today's weekday
    r = client.put(
        "/api/v1/tenants/me/callback-promises",
        headers=viewer,
        json={"callback_promise": "We'll call."},
    )
    assert r.status_code == 403
